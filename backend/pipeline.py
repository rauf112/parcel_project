"""
Pipeline orchestration for parcel envelope generation.

Responsibilities
----------------
- Loads and caches POUM indices.
- Resolves parcel polygon source (POUM, Cadastre, or both).
- Applies zoning rules to compute height/depth and roof constraints.
- Exports IFC envelope files and normalizes output paths.

Data flow
---------
- refcat -> polygon (POUM/Cadastre) -> rules (regulations/POUM/default)
    -> IFC envelope (ifc_exporter) -> normalized output path.

Notes
-----
- Configuration supports layered overrides (default → config.json → ENV override).
- Cadastre WFS failures are handled with fallbacks when possible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import time
import shutil
import math
import json
import os

from pyproj import Transformer

from cadastre_client import get_parcel_polygon_by_local_id
from poum_index import build_refcat_to_poum_index, PoumInfo
import regulations
from ifc_exporter import create_ifc_envelope


# --- Simple cache for POUM index ---
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


# -----------------------------------------------------------------------------
# Configuration loader
# -----------------------------------------------------------------------------

_CONFIG_PATHS = [Path("backend/config.json"), Path("config.json")]


def _load_config() -> dict:
    """Load configuration with layered precedence.

    Precedence (low -> high):
      1) code defaults (hardcoded)
      2) `backend/config.json` (if present)
      3) file pointed by ENV `ENVELOPE_CONFIG_PATH` (if present)

    Keys supported:
      - ground_height: float
      - default_depth_m: float or null
      - force_depth_m: bool
      - debug_depth_log: bool
      - polygon_source: 'poum'|'cadastre'|'both'
      - poum_mode: 'parcel'|'zone'
      - poum_zone_area_ratio_threshold: float
      - poum_simplify_zone: bool
      - poum_simplify_method: string
      - roof_rise_max_m: float
    """
    # Base: code defaults
    defaults = {
        "ground_height": 1.0,
        "default_depth_m": None,
        "force_depth_m": False,
        "debug_depth_log": False,
        "polygon_source": "both",
        "poum_mode": "parcel",
        "poum_zone_area_ratio_threshold": 3.0,
        "poum_simplify_zone": True,
        "poum_simplify_method": "convex_hull",
        "roof_rise_max_m": 10.0,
        "poum_zone_intersection": True,
    }

    # Merge backend/config.json (if exists)
    for cp in _CONFIG_PATHS:
        if cp.exists():
            try:
                cfg = json.loads(cp.read_text())
                if isinstance(cfg, dict):
                    defaults.update(cfg)
            except Exception:
                pass

    # Merge env override file (highest precedence)
    envp = os.environ.get("ENVELOPE_CONFIG_PATH")
    if envp:
        try:
            cfg = json.loads(Path(envp).read_text())
            if isinstance(cfg, dict):
                defaults.update(cfg)
        except Exception:
            pass

    return defaults



def list_refcats_from_poum(poum_gml_path: str) -> List[str]:
    idx = _get_poum_index(poum_gml_path)
    return sorted(idx.keys())


def _utm_epsg_from_lon_lat(lon: float, lat: float) -> int:
    # UTM zone from longitude
    zone = int(math.floor((lon + 180) / 6) + 1)
    # North hemisphere
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
    If an IFC file is written to a subfolder, move it to the output root.
    Deletes the subfolder if it becomes empty.
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
    Priority rules:
        height: regulations → POUM(ALTMAX) → DEFAULT_RULE
        depth : regulations(max_building_depth_m) → POUM(PROFEDIF) → None (fallback BBX)
    Returns (height, depth, rule_sources) where rule_sources records the origin.
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


def _polygon_area(points_xy: List[Tuple[float, float]]) -> float:
    """Return absolute polygon area (m^2)."""
    pts = _ensure_closed(points_xy)
    a = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        a += (x1 * y2 - x2 * y1)
    return abs(0.5 * a)


def generate_one(
    refcat: str,
    parcels_data: Dict[str, Any],
    output_dir: str | Path,
    municipality_slug: str = "malgrat",
    poum_gml_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a single parcel envelope from preprocessed parcels_simplified.json:
    Parcel points (WGS84) → project to UTM → POUM zone → rules → IFC
    
    Parameters:
    -----------
    refcat : str
        Parcel reference (e.g., "000302300DG70H")
    parcels_data : Dict[str, Any]
        Preprocessed data from parcels_simplified.json.
        Structure: {refcat: {id, points: [[lon,lat],...], segments: [...]}}
    output_dir : str | Path
        Where to write IFC files
    municipality_slug : str
        Prefix for output filenames
    poum_gml_path : Optional[str]
        POUM GML file (needed for zone/ALTMAX/PROFEDIF lookups only)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) Try to get parcel data from JSON first
    xy = None
    config = _load_config()
    
    if refcat in parcels_data:
        # Use preprocessed points from JSON (already in UTM)
        parcel_entry = parcels_data[refcat]
        points = parcel_entry.get("points", [])
        
        if points:
            # JSON points are in EPSG:25831 (UTM) already, just convert to tuples
            xy = [tuple(p) if isinstance(p, (list, tuple)) else p for p in points]
            if config.get("debug_depth_log"):
                print(f"[SRC] Using preprocessed UTM points for {refcat} ({len(xy)} vertices)")
    
    # 2) Fallback to cadastre WFS if not in JSON
    if xy is None and refcat not in parcels_data:
        try:
            lonlat = get_parcel_polygon_by_local_id(refcat)
            xy = _transform_wgs84_to_utm(lonlat)
            if config.get("debug_depth_log"):
                print(f"[SRC] Fallback: Using CADASTRE WFS polygon for {refcat}")
        except Exception as e:
            msg = str(e)
            if "<html" in msg.lower() or "text/html" in msg.lower():
                raise RuntimeError("Service unavailable (WFS returned HTML / maintenance).") from e
            raise RuntimeError(f"Parcel {refcat} not found in preprocessed data or cadastre WFS: {e}") from e
    
    if xy is None:
        raise RuntimeError(f"Could not obtain polygon for parcel {refcat}")
    
    xy = _ensure_closed(xy)

    # 3) Get zone info from POUM (if available)
    zone = "UNKNOWN"
    poum_info = None
    
    if poum_gml_path:
        try:
            idx = _get_poum_index(poum_gml_path)
            poum_info = idx.get(refcat)
            if poum_info:
                zone = poum_info.zone
        except Exception as e:
            if config.get("debug_depth_log"):
                print(f"[WARN] Could not load POUM zone for {refcat}: {e}")

    # 4) Rules
    height_m, depth_m, rule_sources = _pick_height_and_depth(zone, poum_info)

    # If a default depth is configured, apply it as fallback; if force_depth_m is True,
    # it overrides any rule-derived depth
    cfg_default = config.get("default_depth_m")
    if config.get("force_depth_m") and cfg_default is not None:
        depth_m = float(cfg_default)
        rule_sources.append("depth:FORCE_CONFIG")
    else:
        if depth_m is None and cfg_default is not None:
            depth_m = float(cfg_default)
            rule_sources.append("depth:CONFIG_DEFAULT")

    # Depth fallback: bounding-box depth
    if depth_m is None:
        depth_m = _bbox_depth(xy)

    # 4) Standard filename
    safe_zone = zone.replace("/", "_").replace("\\", "_").replace(" ", "_")
    out_name = f"{municipality_slug}_{refcat}_{safe_zone}_envelope.ifc"
    out_path = output_dir / out_name

    # 5) IFC export
    real_slope_deg, virtual_slope_deg, roof_sources = _pick_roof_slopes(zone)
    rule_sources.extend(roof_sources)

    # Use configured ground_height if present
    ground_h = config.get("ground_height", 1.0)

    # Optional debug logging for depth decisions
    if config.get("debug_depth_log"):
        print(f"[CONFIG] zone={zone}, rule_depth={depth_m}, sources={rule_sources}")

    create_ifc_envelope(
        ground_footprint_points=xy,  
        footprint_points=xy,
        height=height_m,
        zone_key=zone,
        out_path=str(out_path),
        roof_slope_deg_real=real_slope_deg,
        roof_slope_deg_virtual=virtual_slope_deg,
        ground_height=ground_h,
        depth_m=depth_m,
        max_roof_rise_m=config.get("roof_rise_max_m"),
    )

    # 6) Normalize (in case exporter writes to a subfolder)
    final_path = _normalize_output_to_root(str(out_path), output_dir)

    return {
        "refcat": refcat,
        "zone": zone,
        "ifc_path": final_path,
        "rule_sources": rule_sources,
        "skipped": False,
    }

def _pick_roof_slopes(zone: str) -> Tuple[float, float, list[str]]:
    zc = regulations.canonical_zone(zone)
    zr = regulations.ZONE_RULES.get(zc, regulations.DEFAULT_RULE)

    sources = ["roof:REGULATIONS" if zc in regulations.ZONE_RULES else "roof:DEFAULT_RULE"]

    real = zr.max_roof_slope_deg_real
    virt = zr.max_roof_slope_deg_virtual

    # If zone rule does not define slopes, fall back to defaults
    if real is None:
        real = regulations.DEFAULT_RULE.max_roof_slope_deg_real
        sources.append("roof_real:FALLBACK_DEFAULT")

    if virt is None:
        virt = regulations.DEFAULT_RULE.max_roof_slope_deg_virtual
        sources.append("roof_virtual:FALLBACK_DEFAULT")

    return float(real), float(virt), sources