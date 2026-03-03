from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import math

import ifcopenshell

from config import POUM_GML_PATH, OUTPUT_DIR
from pipeline import generate_one
from ifc_exporter import convex_hull, polygon_intersection


@dataclass
class BBox3D:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float
    sampled_vertices: int

    def lengths(self) -> tuple[float, float, float]:
        return (
            max(0.0, self.max_x - self.min_x),
            max(0.0, self.max_y - self.min_y),
            max(0.0, self.max_z - self.min_z),
        )

    def volume(self) -> float:
        lx, ly, lz = self.lengths()
        return float(lx * ly * lz)


def _placement_translation(placement: Any) -> tuple[float, float, float]:
    tx, ty, tz = 0.0, 0.0, 0.0
    current = placement
    safety = 0
    while current is not None and safety < 200:
        safety += 1
        try:
            rel = getattr(current, "RelativePlacement", None)
            loc = getattr(rel, "Location", None) if rel is not None else None
            coords = getattr(loc, "Coordinates", None) if loc is not None else None
            if coords:
                tx += float(coords[0]) if len(coords) >= 1 else 0.0
                ty += float(coords[1]) if len(coords) >= 2 else 0.0
                tz += float(coords[2]) if len(coords) >= 3 else 0.0
            current = getattr(current, "PlacementRelTo", None)
        except Exception:
            break
    return tx, ty, tz


def _iter_children(obj: Any):
    if obj is None:
        return
    if isinstance(obj, (list, tuple)):
        for it in obj:
            yield it
        return
    try:
        if hasattr(obj, "is_a"):
            info = obj.get_info(recursive=False)
            for key, value in info.items():
                if key in {"id", "type"}:
                    continue
                yield value
    except Exception:
        return


def _collect_points(obj: Any, out: list[tuple[float, float, float]], visited: set[int]) -> None:
    if obj is None:
        return
    if isinstance(obj, (list, tuple)):
        for it in obj:
            _collect_points(it, out, visited)
        return

    if not hasattr(obj, "is_a"):
        return

    try:
        obj_id = obj.id()
        if obj_id in visited:
            return
        visited.add(obj_id)
    except Exception:
        pass

    try:
        if obj.is_a("IfcCartesianPoint"):
            coords = getattr(obj, "Coordinates", None) or []
            x = float(coords[0]) if len(coords) >= 1 else 0.0
            y = float(coords[1]) if len(coords) >= 2 else 0.0
            z = float(coords[2]) if len(coords) >= 3 else 0.0
            out.append((x, y, z))
            return
    except Exception:
        return

    for child in _iter_children(obj):
        _collect_points(child, out, visited)


def _extract_world_vertices(
    model: ifcopenshell.file,
    products: Optional[list[Any]] = None,
) -> list[tuple[float, float, float]]:
    world_points: list[tuple[float, float, float]] = []
    iterable = products if products is not None else model.by_type("IfcProduct")

    for product in iterable:
        representation = getattr(product, "Representation", None)
        if representation is None:
            continue

        local_points: list[tuple[float, float, float]] = []
        visited_local: set[int] = set()
        _collect_points(representation, local_points, visited_local)
        if not local_points:
            continue

        px, py, pz = _placement_translation(getattr(product, "ObjectPlacement", None))
        for lx, ly, lz in local_points:
            world_points.append((float(lx + px), float(ly + py), float(lz + pz)))

    return world_points


def _bbox_from_points(points: list[tuple[float, float, float]]) -> BBox3D:
    if not points:
        raise RuntimeError("No points available to compute bounding box.")

    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    min_z = min(p[2] for p in points)
    max_x = max(p[0] for p in points)
    max_y = max(p[1] for p in points)
    max_z = max(p[2] for p in points)

    return BBox3D(
        min_x=min_x,
        min_y=min_y,
        min_z=min_z,
        max_x=max_x,
        max_y=max_y,
        max_z=max_z,
        sampled_vertices=len(points),
    )


def _polygon_area(points_xy: list[tuple[float, float]]) -> float:
    if len(points_xy) < 3:
        return 0.0
    area2 = 0.0
    n = len(points_xy)
    for i in range(n):
        x1, y1 = points_xy[i]
        x2, y2 = points_xy[(i + 1) % n]
        area2 += (x1 * y2 - x2 * y1)
    return abs(0.5 * area2)


def _iter_storey_products(storey: Any):
    rels = getattr(storey, "ContainsElements", None) or []
    for rel in rels:
        for el in (getattr(rel, "RelatedElements", None) or []):
            yield el


def _pick_ground_storey(model: ifcopenshell.file) -> Optional[Any]:
    storeys = list(model.by_type("IfcBuildingStorey"))
    if not storeys:
        return None

    def _rank(s: Any) -> tuple[int, float]:
        name = str(getattr(s, "Name", "") or "").lower()
        is_ground_name = any(k in name for k in ["ground", "planta baja", "rez", "rc"])
        elev_raw = getattr(s, "Elevation", None)
        try:
            elev = float(elev_raw) if elev_raw is not None else 1e9
        except Exception:
            elev = 1e9
        name_rank = 0 if is_ground_name else 1
        return name_rank, elev

    return sorted(storeys, key=_rank)[0]


def _is_main_building_product(product: Any) -> bool:
    try:
        cls = str(product.is_a())
    except Exception:
        return False

    excluded_prefixes = (
        "IfcFlow",
        "IfcDistribution",
        "IfcFurnishing",
        "IfcAnnotation",
        "IfcOpening",
        "IfcVirtual",
    )
    excluded_exact = {
        "IfcSpace",
        "IfcGrid",
        "IfcSite",
        "IfcBuilding",
        "IfcBuildingStorey",
    }
    if cls in excluded_exact:
        return False
    if any(cls.startswith(pref) for pref in excluded_prefixes):
        return False
    return True


def _build_ground_perimeter_temp_volume(path: Path) -> Dict[str, Any]:
    model = ifcopenshell.open(str(path))
    warnings: list[str] = []

    all_points = _extract_world_vertices(model)
    if not all_points:
        raise RuntimeError(f"No geometric vertices could be extracted from IFC: {path}")

    ground_storey = _pick_ground_storey(model)
    ground_storey_name = str(getattr(ground_storey, "Name", "")) if ground_storey is not None else None

    storey_points: list[tuple[float, float, float]] = []
    if ground_storey is not None:
        storey_products = list(_iter_storey_products(ground_storey))
        if storey_products:
            storey_points = _extract_world_vertices(model, storey_products)

    if not storey_points:
        all_min_z = min(p[2] for p in all_points)
        z_band_m = 0.60
        storey_points = [p for p in all_points if p[2] <= all_min_z + z_band_m]
        warnings.append("Ground-storey relations were not found; used lowest-z band fallback for footprint extraction.")

    if len(storey_points) < 3:
        raise RuntimeError("Ground-floor perimeter extraction failed: insufficient vertices.")

    footprint_xy = [(p[0], p[1]) for p in storey_points]
    footprint_hull = convex_hull(footprint_xy)
    if len(footprint_hull) < 3:
        raise RuntimeError("Ground-floor perimeter extraction failed: footprint hull is degenerate.")

    main_products = [p for p in model.by_type("IfcProduct") if _is_main_building_product(p)]
    main_points = _extract_world_vertices(model, main_products) if main_products else []
    if not main_points:
        main_points = all_points
        warnings.append("Main-building product filtering yielded no vertices; using all IFC product vertices for height.")

    z_min = min(p[2] for p in storey_points)
    z_max = max(p[2] for p in main_points)
    if z_max <= z_min:
        z_min = min(p[2] for p in all_points)
        z_max = max(p[2] for p in all_points)

    height = max(0.0, z_max - z_min)
    if height <= 0.0:
        raise RuntimeError("Temporary volume height is zero or invalid.")

    xs = [p[0] for p in footprint_hull]
    ys = [p[1] for p in footprint_hull]

    return {
        "footprint_hull": footprint_hull,
        "min_x": min(xs),
        "min_y": min(ys),
        "max_x": max(xs),
        "max_y": max(ys),
        "z_min": z_min,
        "z_max": z_max,
        "height_m": height,
        "ground_storey": ground_storey_name,
        "sampled_vertices_ground": len(storey_points),
        "sampled_vertices_height": len(main_points),
        "warnings": warnings,
    }


def _bbox_from_ifc(path: Path) -> BBox3D:
    if not path.exists():
        raise FileNotFoundError(f"IFC file not found: {path}")

    model = ifcopenshell.open(str(path))

    points = _extract_world_vertices(model)
    if not points:
        raise RuntimeError(f"No geometric vertices could be extracted from IFC: {path}")
    return _bbox_from_points(points)


def _intersection_volume(a: BBox3D, b: BBox3D) -> float:
    ix = max(0.0, min(a.max_x, b.max_x) - max(a.min_x, b.min_x))
    iy = max(0.0, min(a.max_y, b.max_y) - max(a.min_y, b.min_y))
    iz = max(0.0, min(a.max_z, b.max_z) - max(a.min_z, b.min_z))
    return float(ix * iy * iz)


def _overflow_by_side(project: BBox3D, allowed: BBox3D) -> Dict[str, float]:
    return {
        "west": max(0.0, allowed.min_x - project.min_x),
        "east": max(0.0, project.max_x - allowed.max_x),
        "south": max(0.0, allowed.min_y - project.min_y),
        "north": max(0.0, project.max_y - allowed.max_y),
        "down": max(0.0, allowed.min_z - project.min_z),
        "up": max(0.0, project.max_z - allowed.max_z),
    }


def _bbox_dict(b: BBox3D) -> Dict[str, Any]:
    lx, ly, lz = b.lengths()
    return {
        "min": {"x": b.min_x, "y": b.min_y, "z": b.min_z},
        "max": {"x": b.max_x, "y": b.max_y, "z": b.max_z},
        "size": {"x": lx, "y": ly, "z": lz},
        "bbox_volume_m3": b.volume(),
        "sampled_vertices": b.sampled_vertices,
    }


def run_volume_compliance_check(
    *,
    municipality: str,
    refcat: str,
    architect_ifc_path: str,
    tolerance_m: float = 0.01,
    keep_allowed_ifc: bool = True,
) -> Dict[str, Any]:
    municipality_slug = "malgrat" if municipality == "Malgrat de Mar" else municipality.lower().replace(" ", "_")

    result = generate_one(
        refcat=refcat,
        poum_gml_path=str(POUM_GML_PATH),
        output_dir=OUTPUT_DIR,
        municipality_slug=municipality_slug,
    )

    if result.get("skipped"):
        raise ValueError(f"Allowed envelope could not be generated for refcat={refcat} (skipped/non-buildable).")
    if not result.get("ifc_path"):
        raise ValueError(f"Allowed envelope IFC path is missing for refcat={refcat}.")

    allowed_ifc_path = Path(result["ifc_path"]).resolve()
    architect_path = Path(architect_ifc_path).expanduser().resolve()

    allowed_bbox = _bbox_from_ifc(allowed_ifc_path)
    warnings: list[str] = []

    try:
        temp_volume = _build_ground_perimeter_temp_volume(architect_path)

        project_bbox = BBox3D(
            min_x=float(temp_volume["min_x"]),
            min_y=float(temp_volume["min_y"]),
            min_z=float(temp_volume["z_min"]),
            max_x=float(temp_volume["max_x"]),
            max_y=float(temp_volume["max_y"]),
            max_z=float(temp_volume["z_max"]),
            sampled_vertices=int(temp_volume["sampled_vertices_ground"]),
        )

        footprint_hull: list[tuple[float, float]] = temp_volume["footprint_hull"]
        height_m = float(temp_volume["height_m"])

        allowed_rect = [
            (allowed_bbox.min_x, allowed_bbox.min_y),
            (allowed_bbox.max_x, allowed_bbox.min_y),
            (allowed_bbox.max_x, allowed_bbox.max_y),
            (allowed_bbox.min_x, allowed_bbox.max_y),
        ]

        project_area = _polygon_area(footprint_hull)
        inter_poly = polygon_intersection(footprint_hull, allowed_rect)
        intersection_area = _polygon_area(inter_poly) if inter_poly else 0.0
        outside_area = max(0.0, project_area - intersection_area)

        project_volume = float(project_area * height_m)
        intersection_volume = float(intersection_area * height_m)
        outside_volume = float(outside_area * height_m)

        method = "ground_floor_perimeter_temp_volume"
        warnings.extend(temp_volume.get("warnings", []))
        if temp_volume.get("ground_storey"):
            warnings.append(f"Ground-storey used for footprint extraction: {temp_volume.get('ground_storey')}")
        warnings.append("Temporary compliance volume is derived from ground-floor perimeter and is not persisted as IFC.")
        warnings.append("Topology/boolean solid intersections are not used in this check.")

    except Exception as e:
        project_bbox = _bbox_from_ifc(architect_path)
        project_volume = project_bbox.volume()
        intersection_volume = _intersection_volume(project_bbox, allowed_bbox)
        outside_volume = max(0.0, project_volume - intersection_volume)
        method = "bbox"
        warnings.append(f"Ground-floor temporary volume extraction failed; fallback to bbox method. reason={type(e).__name__}: {e}")
        warnings.append("MVP bbox method: this is an approximate geometric compliance result.")
        warnings.append("Topology/boolean solid intersections are not used in this check.")

    tolerance_value = float(tolerance_m)
    overflow = _overflow_by_side(project_bbox, allowed_bbox)
    overflow_exceeds_tolerance_by_side = {
        side: float(distance) > tolerance_value for side, distance in overflow.items()
    }
    exceeds = any(overflow_exceeds_tolerance_by_side.values())

    response: Dict[str, Any] = {
        "compliant": not exceeds,
        "method": method,
        "municipality": municipality,
        "refcat": refcat,
        "tolerance": {
            "value_m": tolerance_value,
            "rule": "A side is considered non-compliant when overflow_by_side_m[side] > tolerance.value_m.",
            "overflow_exceeds_tolerance_by_side": overflow_exceeds_tolerance_by_side,
            "max_overflow_m": max(overflow.values()) if overflow else 0.0,
        },
        "rule_zone": result.get("zone"),
        "overflow_by_side_m": overflow,
        "volumes": {
            "project": project_volume,
            "intersection": intersection_volume,
            "outside": outside_volume,
        },
        "warnings": warnings,
        "allowed_bbox": _bbox_dict(allowed_bbox),
        "project_bbox": _bbox_dict(project_bbox),
        "sources": {
            "allowed_ifc": str(allowed_ifc_path),
            "architect_ifc": str(architect_path),
            "keep_allowed_ifc": bool(keep_allowed_ifc),
        },
    }

    if not keep_allowed_ifc:
        try:
            allowed_ifc_path.unlink(missing_ok=True)
        except Exception:
            pass

    return response
