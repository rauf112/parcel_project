from __future__ import annotations

import json
import math
import ast
import importlib.util
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

from pyproj import Transformer

from cadastre_client import get_parcel_polygon_by_local_id
from poum_index import build_refcat_to_poum_index, get_polygon_by_refcat

Point2 = Tuple[float, float]


def _utm_epsg_from_lon_lat(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180) / 6) + 1)
    return 32600 + zone if lat >= 0 else 32700 + zone


def _transform_wgs84_to_utm(points_lonlat: List[Tuple[float, float]]) -> List[Point2]:
    if not points_lonlat:
        return []
    lon0, lat0 = points_lonlat[0]
    epsg = _utm_epsg_from_lon_lat(lon0, lat0)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    return [tuple(map(float, transformer.transform(lon, lat))) for lon, lat in points_lonlat]


def _ensure_open(points: List[Point2]) -> List[Point2]:
    if len(points) >= 2 and points[0] == points[-1]:
        return points[:-1]
    return points


def _load_urban_microservice_obtain_streets():
    """
    Load ACCORD Urban-Regulation-Microservice preprocessing utility.
    Returns (obtain_streets_callable, loaded_module).
    Raises RuntimeError if unavailable or invalid.
    """
    this_dir = Path(__file__).resolve().parent
    root = this_dir.parent
    utils_path = root / "Urban-Regulation-Microservice" / "Urban-Regulation-Microservice" / "apps" / "Preprocessing" / "utils.py"

    if not utils_path.exists():
        raise RuntimeError(
            "Urban-Regulation-Microservice not found at expected path: "
            "Urban-Regulation-Microservice/Urban-Regulation-Microservice/apps/Preprocessing/utils.py"
        )

    try:
        spec = importlib.util.spec_from_file_location("urban_reg_preprocess_utils", str(utils_path))
        if spec is None or spec.loader is None:
            raise RuntimeError("Unable to load Urban-Regulation-Microservice preprocessing module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        obtain_streets = getattr(module, "obtain_streets", None)
        if not callable(obtain_streets):
            raise RuntimeError("Urban-Regulation-Microservice loaded but obtain_streets is not callable")
        return obtain_streets, module
    except Exception as e:
        raise RuntimeError(f"Failed loading Urban-Regulation-Microservice preprocessing utility: {e}") from e


def _load_urban_microservice_simplify_algorithms():
    """
    Load simplify helper algorithms directly from Urban-Regulation-Microservice
    source file `apps/Preprocessing/views/create_simplified_cadastre.py`.

    Returns callables:
      - filter_vertices(points, angle_threshold)
      - calculate_segments(points)
      - calculate_segment_length(segment)
    """
    this_dir = Path(__file__).resolve().parent
    root = this_dir.parent
    src_path = root / "Urban-Regulation-Microservice" / "Urban-Regulation-Microservice" / "apps" / "Preprocessing" / "views" / "create_simplified_cadastre.py"

    if not src_path.exists():
        raise RuntimeError(
            "Urban-Regulation-Microservice simplify source not found at expected path: "
            "Urban-Regulation-Microservice/Urban-Regulation-Microservice/apps/Preprocessing/views/create_simplified_cadastre.py"
        )

    try:
        source = src_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(src_path))
        needed = {"calculate_angle", "calculate_angles", "filter_vertices", "calculate_segments", "calculate_segment_length"}

        selected_defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in needed]
        if len(selected_defs) < len(needed):
            missing = sorted(list(needed - {n.name for n in selected_defs}))
            raise RuntimeError(f"Missing expected functions in microservice simplify source: {missing}")

        module_ast = ast.Module(body=selected_defs, type_ignores=[])
        compiled = compile(module_ast, filename=str(src_path), mode="exec")

        import numpy as np

        ns: Dict[str, Any] = {"np": np}
        exec(compiled, ns, ns)

        fv = ns.get("filter_vertices")
        cs = ns.get("calculate_segments")
        csl = ns.get("calculate_segment_length")
        if not (callable(fv) and callable(cs) and callable(csl)):
            raise RuntimeError("Loaded microservice simplify functions are not callable")
        return fv, cs, csl
    except Exception as e:
        raise RuntimeError(f"Failed loading Urban-Regulation-Microservice simplify algorithms: {e}") from e


def _get_polygon_for_refcat(refcat: str, poum_gml_path: str, source: str, poum_mode: str) -> Optional[List[Point2]]:
    if source in ("poum", "both"):
        strict = poum_mode == "parcel"
        p = get_polygon_by_refcat(poum_gml_path, refcat, strict=strict)
        if p:
            return _ensure_open(p)

    if source in ("cadastre", "both"):
        lonlat = get_parcel_polygon_by_local_id(refcat)
        if lonlat:
            return _ensure_open(_transform_wgs84_to_utm(lonlat))

    return None


def generate_simplified_cadastre_like_file(
    poum_gml_path: str,
    output_file_path: str,
    source: str = "both",
    poum_mode: str = "parcel",
    max_distance: float = 30.0,
    offset_distance: float = 0.1,
    angle_threshold: float = 0.1,
) -> Dict[str, Any]:
    idx = build_refcat_to_poum_index(poum_gml_path)
    refcats = sorted(idx.keys())

    filter_vertices, calculate_segments, calculate_segment_length = _load_urban_microservice_simplify_algorithms()

    parcels_data: Dict[str, Any] = {}
    for refcat in refcats:
        try:
            points = _get_polygon_for_refcat(refcat, poum_gml_path, source=source, poum_mode=poum_mode)
        except Exception:
            points = None

        if not points or len(points) < 3:
            continue

        points = _ensure_open(points)
        vertices = filter_vertices(points, angle_threshold=angle_threshold)
        segments = calculate_segments(vertices)

        segments_data = []
        for i, seg in enumerate(segments):
            segments_data.append(
                {
                    "id": f"{refcat}_{i}",
                    "segment": [[float(seg[0][0]), float(seg[0][1])], [float(seg[1][0]), float(seg[1][1])]],
                    "length": float(calculate_segment_length(seg)),
                }
            )

        parcels_data[refcat] = {
            "id": refcat,
            "points": [[float(x), float(y)] for x, y in points],
            "segments": segments_data,
        }

    # Street inference strictly via Urban-Regulation-Microservice functionality
    obtain_streets, module = _load_urban_microservice_obtain_streets()
    module.MAX_DISTANCE = float(max_distance)
    module.OFFSET_DISTANCE = float(offset_distance)
    obtain_streets(parcels_data)

    output_path = Path(output_file_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(parcels_data, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "output_file": str(output_path),
        "parcel_count": len(parcels_data),
        "street_engine_used": "urban_microservice",
    }
