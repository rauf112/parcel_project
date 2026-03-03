from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import math

import ifcopenshell

from config import POUM_GML_PATH, OUTPUT_DIR
from pipeline import generate_one


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


def _bbox_from_ifc(path: Path) -> BBox3D:
    if not path.exists():
        raise FileNotFoundError(f"IFC file not found: {path}")

    model = ifcopenshell.open(str(path))

    min_x = min_y = min_z = math.inf
    max_x = max_y = max_z = -math.inf
    sampled_vertices = 0

    visited_global: set[int] = set()

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

    def _collect_points(obj: Any, out: list[tuple[float, float, float]]) -> None:
        if obj is None:
            return
        if isinstance(obj, (list, tuple)):
            for it in obj:
                _collect_points(it, out)
            return

        if not hasattr(obj, "is_a"):
            return

        try:
            obj_id = obj.id()
            if obj_id in visited_global:
                return
            visited_global.add(obj_id)
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
            _collect_points(child, out)

    for product in model.by_type("IfcProduct"):
        representation = getattr(product, "Representation", None)
        if representation is None:
            continue

        local_points: list[tuple[float, float, float]] = []
        _collect_points(representation, local_points)
        if not local_points:
            continue

        px, py, pz = _placement_translation(getattr(product, "ObjectPlacement", None))

        for lx, ly, lz in local_points:
            x = float(lx + px)
            y = float(ly + py)
            z = float(lz + pz)

            min_x = min(min_x, x)
            min_y = min(min_y, y)
            min_z = min(min_z, z)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
            max_z = max(max_z, z)
            sampled_vertices += 1

    if sampled_vertices == 0:
        raise RuntimeError(f"No geometric vertices could be extracted from IFC: {path}")

    return BBox3D(
        min_x=min_x,
        min_y=min_y,
        min_z=min_z,
        max_x=max_x,
        max_y=max_y,
        max_z=max_z,
        sampled_vertices=sampled_vertices,
    )


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

    allowed_ifc_path = Path(result["ifc_path"]).resolve()
    architect_path = Path(architect_ifc_path).expanduser().resolve()

    allowed_bbox = _bbox_from_ifc(allowed_ifc_path)
    project_bbox = _bbox_from_ifc(architect_path)

    overflow = _overflow_by_side(project_bbox, allowed_bbox)
    exceeds = any(v > float(tolerance_m) for v in overflow.values())

    intersection_volume = _intersection_volume(project_bbox, allowed_bbox)
    project_bbox_volume = project_bbox.volume()
    outside_volume = max(0.0, project_bbox_volume - intersection_volume)

    response: Dict[str, Any] = {
        "compliant": not exceeds,
        "municipality": municipality,
        "refcat": refcat,
        "tolerance_m": float(tolerance_m),
        "rule_zone": result.get("zone"),
        "overflow_by_side_m": overflow,
        "volumes": {
            "project_bbox_m3": project_bbox_volume,
            "intersection_bbox_m3": intersection_volume,
            "outside_bbox_m3": outside_volume,
        },
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
