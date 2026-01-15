from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import time
import shutil
import math

from pyproj import Transformer

from cadastre_client import get_parcel_polygon_by_local_id
from poum_index import build_refcat_to_poum_index, PoumInfo
import regulations
from ifc_exporter import create_ifc_envelope


# --- simple cache for POUM index ---
_POUM_CACHE: Dict[str, Any] = {
    "path": None,
    "mtime": None,
    "index": None,
}


def _get_poum_index(poum_gml_path: str) -> Dict[str, PoumInfo]:
    p = Path(poum_gml_path)
    mtime = p.stat().st_mtime

    if _POUM_CACHE["path"] != str(p.resolve()) or _POUM_CACHE["mtime"] != mtime or _POUM_CACHE["index"] is None:
        idx = build_refcat_to_poum_index(str(p))
        _POUM_CACHE["path"] = str(p.resolve())
        _POUM_CACHE["mtime"] = mtime
        _POUM_CACHE["index"] = idx
    return _POUM_CACHE["index"]


def list_refcats_from_poum(poum_gml_path: str) -> List[str]:
    idx = _get_poum_index(poum_gml_path)
    return sorted(idx.keys())


def _utm_epsg_from_lon_lat(lon: float, lat: float) -> int:
    # UTM zone from longitude
    zone = int(math.floor((lon + 180) / 6) + 1)
    # north hemisphere
    return 32600 + zone if lat >= 0 else 32700 + zone


def _transform_wgs84_to_utm(points_lonlat: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not points_lonlat:
        raise ValueError("Empty polygon points")

    lon0, lat0 = points_lonlat[0]
    epsg = _utm_epsg_from_lon_lat(lon0, lat0)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    out = []
    for lon, lat in points_lonlat:
        x, y = transformer.transform(lon, lat)
        out.append((float(x), float(y)))
    return out


def _ensure_closed(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not points:
        return points
    if points[0] != points[-1]:
        return points + [points[0]]
    return points


def _normalize_output_to_root(ifc_path: str, output_dir: Path) -> str:
    """
    IFC alt klasöre yazılmışsa outputs/ köküne taşır.
    Alt klasör boş kalırsa siler.
    """
    p = Path(ifc_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if p.parent.resolve() == output_dir.resolve():
        return str(p)

    target = output_dir / p.name

    if target.exists():
        stem, suf = target.stem, target.suffix
        k = 2
        while True:
            cand = output_dir / f"{stem}_{k}{suf}"
            if not cand.exists():
                target = cand
                break
            k += 1

    shutil.move(str(p), str(target))

    # try remove parent if empty
    try:
        parent = p.parent
        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass

    return str(target)


def _pick_height_and_depth(zone: str, poum_info: Optional[PoumInfo]) -> tuple[float, Optional[float], list[str]]:
    """
    Öncelik:
      height: regulations varsa REGULATIONS, yoksa POUM(ALTMAX), yoksa DEFAULT_RULE
      depth: regulations varsa REGULATIONS (max_building_depth_m),
             yoksa POUM(PROFEDIF),
             yoksa None -> fallback BBX (main tarafı 'BBX' diye logluyordu)
    """
    rule_sources: list[str] = []

    zc = regulations.canonical_zone(zone)
    has_reg = zc in regulations.ZONE_RULES

    # HEIGHT
    if has_reg:
        h = regulations.ZONE_RULES[zc].max_reg_height_m
        rule_sources.append("height:REGULATIONS")
    else:
        if poum_info and poum_info.altmax:
            h = float(poum_info.altmax)
            rule_sources.append("height:POUM(ALTMAX)")
        else:
            h = regulations.DEFAULT_RULE.max_reg_height_m
            rule_sources.append("height:DEFAULT_RULE")

    # DEPTH
    d: Optional[float] = None
    if has_reg:
        d = regulations.ZONE_RULES[zc].max_building_depth_m
        if d is not None:
            rule_sources.append("depth:REGULATIONS")
    if d is None:
        if poum_info and poum_info.profedif:
            d = float(poum_info.profedif)
            rule_sources.append("depth:POUM(PROFEDIF)")
        else:
            # fallback later: bounding-box depth
            rule_sources.append("depth:BBX")

    return h, d, rule_sources


def _bbox_depth(points_xy: List[Tuple[float, float]]) -> float:
    xs = [p[0] for p in points_xy]
    ys = [p[1] for p in points_xy]
    return float(max(max(xs) - min(xs), max(ys) - min(ys)))


def generate_one(
    refcat: str,
    poum_gml_path: str,
    output_dir: str | Path,
    municipality_slug: str = "malgrat",
) -> Dict[str, Any]:
    """
    Tek parsel için:
    WFS -> polygon -> POUM zone -> (regulations or POUM numeric) -> IFC
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    idx = _get_poum_index(poum_gml_path)
    poum_info = idx.get(refcat)

    zone = (poum_info.zone if poum_info else None) or "UNKNOWN"

    # 1) WFS polygon (WGS84)
    try:
        lonlat = get_parcel_polygon_by_local_id(refcat)
    except Exception as e:
        msg = str(e)
        # WFS bakım/HTML vs
        if "<html" in msg.lower() or "text/html" in msg.lower():
            raise RuntimeError("Service unavailable (WFS returned HTML / maintenance).") from e
        raise

    # 2) Project to meters
    xy = _transform_wgs84_to_utm(lonlat)
    xy = _ensure_closed(xy)

    # 3) rules
    height_m, depth_m, rule_sources = _pick_height_and_depth(zone, poum_info)

    # depth fallback: bbx
    if depth_m is None:
        depth_m = _bbox_depth(xy)

    # 4) Standard filename (tek tip)
    safe_zone = zone.replace("/", "_").replace("\\", "_").replace(" ", "_")
    out_name = f"{municipality_slug}_{refcat}_{safe_zone}_envelope.ifc"
    out_path = output_dir / out_name

    # 5) IFC export
    create_ifc_envelope(
        footprint_points=xy,
        height=height_m,
        zone_key=zone,
        out_path=str(out_path),
    )

    # 6) normalize (olur da exporter alt klasöre yazarsa)
    final_path = _normalize_output_to_root(str(out_path), output_dir)

    return {
        "refcat": refcat,
        "zone": zone,
        "ifc_path": final_path,
        "rule_sources": rule_sources,
        "skipped": False,
    }
