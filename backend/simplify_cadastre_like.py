from __future__ import annotations

import json
import math
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


def _ensure_closed(points: List[Point2]) -> List[Point2]:
    if not points:
        return points
    if points[0] != points[-1]:
        return points + [points[0]]
    return points


def _segment_length(seg: Tuple[Point2, Point2]) -> float:
    (x1, y1), (x2, y2) = seg
    return float(math.hypot(x2 - x1, y2 - y1))


def _calculate_angle(p1: Point2, p2: Point2, p3: Point2) -> float:
    v1 = (p2[0] - p1[0], p2[1] - p1[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])
    n1 = math.hypot(v1[0], v1[1])
    n2 = math.hypot(v2[0], v2[1])
    if n1 == 0.0 or n2 == 0.0:
        return 0.0
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    c = max(-1.0, min(1.0, dot / (n1 * n2)))
    return float(math.acos(c))


def _filter_vertices(points: List[Point2], angle_threshold: float) -> List[Point2]:
    pts = _ensure_open(points)
    if len(pts) < 3:
        return pts
    out: List[Point2] = []
    n = len(pts)
    for i in range(n):
        p1 = pts[i - 1]
        p2 = pts[i]
        p3 = pts[(i + 1) % n]
        ang = _calculate_angle(p1, p2, p3)
        if ang > angle_threshold:
            out.append(p2)
    return out if len(out) >= 3 else pts


def _centroid(points: List[Point2]) -> Point2:
    pts = _ensure_open(points)
    if not pts:
        return (0.0, 0.0)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    n = len(pts)
    return (sx / n, sy / n)


def _outward_normal(seg: Tuple[Point2, Point2], centroid: Point2) -> Point2:
    (x1, y1), (x2, y2) = seg
    dx, dy = (x2 - x1, y2 - y1)
    n1 = (-dy, dx)
    n2 = (dy, -dx)
    mx, my = ((x1 + x2) * 0.5, (y1 + y2) * 0.5)
    to_c = (centroid[0] - mx, centroid[1] - my)

    dot1 = n1[0] * to_c[0] + n1[1] * to_c[1]
    nx, ny = n2 if dot1 > 0 else n1
    norm = math.hypot(nx, ny)
    if norm == 0.0:
        return (0.0, 0.0)
    return (nx / norm, ny / norm)


def _cross(a: Point2, b: Point2) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _ray_segment_intersection_distance(origin: Point2, direction: Point2, a: Point2, b: Point2, max_distance: float) -> Optional[float]:
    seg = (b[0] - a[0], b[1] - a[1])
    denom = _cross(direction, seg)
    if abs(denom) < 1e-12:
        return None

    ao = (a[0] - origin[0], a[1] - origin[1])
    t = _cross(ao, seg) / denom
    u = _cross(ao, direction) / denom

    if t < 0:
        return None
    if u < 0 or u > 1:
        return None

    dist = t * math.hypot(direction[0], direction[1])
    if dist > max_distance:
        return None
    return float(dist)


def _segments_from_points(points: List[Point2]) -> List[Tuple[Point2, Point2]]:
    pts = _ensure_open(points)
    if len(pts) < 2:
        return []
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


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

    parcels_data: Dict[str, Any] = {}
    polygons_by_id: Dict[str, List[Point2]] = {}

    for refcat in refcats:
        try:
            points = _get_polygon_for_refcat(refcat, poum_gml_path, source=source, poum_mode=poum_mode)
        except Exception:
            points = None

        if not points or len(points) < 3:
            continue

        points = _ensure_open(points)
        vertices = _filter_vertices(points, angle_threshold=angle_threshold)
        segments = _segments_from_points(vertices)

        segments_data = []
        for i, seg in enumerate(segments):
            segments_data.append(
                {
                    "id": f"{refcat}_{i}",
                    "segment": [[float(seg[0][0]), float(seg[0][1])], [float(seg[1][0]), float(seg[1][1])]],
                    "length": _segment_length(seg),
                }
            )

        parcels_data[refcat] = {
            "id": refcat,
            "points": [[float(x), float(y)] for x, y in points],
            "segments": segments_data,
        }
        polygons_by_id[refcat] = points

    # Infer street-like widths (similar to ACCORD, not identical)
    for refcat, parcel in parcels_data.items():
        current_points = polygons_by_id[refcat]
        c = _centroid(current_points)

        for seg_data in parcel["segments"]:
            p0 = tuple(seg_data["segment"][0])
            p1 = tuple(seg_data["segment"][1])
            seg = (p0, p1)

            if seg_data["length"] <= 0.1:
                continue

            n = _outward_normal(seg, c)
            if n == (0.0, 0.0):
                continue

            mx = (p0[0] + p1[0]) * 0.5
            my = (p0[1] + p1[1]) * 0.5
            origin = (mx - n[0] * offset_distance, my - n[1] * offset_distance)
            direction = (n[0] * max_distance, n[1] * max_distance)

            min_dist: Optional[float] = None

            for other_id, other_points in polygons_by_id.items():
                if other_id == refcat:
                    continue

                for edge in _segments_from_points(other_points):
                    d = _ray_segment_intersection_distance(origin, direction, edge[0], edge[1], max_distance=max_distance)
                    if d is not None and (min_dist is None or d < min_dist):
                        min_dist = d

            if min_dist is None:
                seg_data["street"] = float(max_distance)
            elif min_dist > 0.1:
                seg_data["street"] = round(float(min_dist), 2)

    output_path = Path(output_file_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(parcels_data, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "output_file": str(output_path),
        "parcel_count": len(parcels_data),
    }
