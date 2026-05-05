from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import math

import numpy as np
import ifcopenshell
import ifcopenshell.util.placement

try:
    import ifcopenshell.geom as _ifc_geom  # type: ignore
except Exception:  # pragma: no cover - environment without geom kernel
    _ifc_geom = None  # type: ignore

try:
    from shapely.geometry import Polygon as _ShPolygon  # type: ignore
    from shapely.ops import unary_union as _sh_unary_union  # type: ignore
    _HAS_SHAPELY = True
except Exception:  # pragma: no cover
    _HAS_SHAPELY = False

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

        # Use full 4x4 transform (translation + rotation) from placement chain
        placement = getattr(product, "ObjectPlacement", None)
        if placement is not None:
            try:
                matrix = ifcopenshell.util.placement.get_local_placement(placement)
            except Exception:
                px, py, pz = _placement_translation(placement)
                matrix = np.eye(4)
                matrix[0, 3] = px
                matrix[1, 3] = py
                matrix[2, 3] = pz
        else:
            matrix = np.eye(4)

        for lx, ly, lz in local_points:
            pt = np.array([lx, ly, lz, 1.0])
            wp = matrix @ pt
            world_points.append((float(wp[0]), float(wp[1]), float(wp[2])))

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


def _find_first_extrusion(node: Any) -> Any:
    """Walk a CSG tree and return the first IfcExtrudedAreaSolid found."""
    if node is None:
        return None
    try:
        if node.is_a("IfcExtrudedAreaSolid"):
            return node
        if node.is_a("IfcBooleanResult") or node.is_a("IfcBooleanClippingResult"):
            r = _find_first_extrusion(node.FirstOperand)
            if r is not None:
                return r
            return _find_first_extrusion(node.SecondOperand)
    except Exception:
        pass
    return None


def _extract_envelope_profile_footprint(path: Path) -> Optional[Dict[str, Any]]:
    """Extract the base footprint polygon from a BUILDING_ENVELOPE element.

    Handles two geometry cases:
    1. CSG (IfcBooleanResult): extracts the base IfcExtrudedAreaSolid profile.
    2. FacetedBrep: extracts the bottom (lowest horizontal) face directly.

    Returns a dict similar to _build_ground_perimeter_temp_volume or None if no
    suitable geometry is found. This avoids the convex-hull contamination from
    hip-roof CSG clipping planes or concave bottom-face expansion.
    """
    model = ifcopenshell.open(str(path))

    envelope_product = None
    for p in model.by_type("IfcBuildingElementProxy"):
        if str(getattr(p, "ObjectType", "") or "").upper() == "BUILDING_ENVELOPE":
            envelope_product = p
            break
    if envelope_product is None:
        return None

    # Get the world-transform matrix for this product
    placement = getattr(envelope_product, "ObjectPlacement", None)
    if placement is not None:
        try:
            matrix = ifcopenshell.util.placement.get_local_placement(placement)
        except Exception:
            px, py, pz = _placement_translation(placement)
            matrix = np.eye(4)
            matrix[0, 3] = px
            matrix[1, 3] = py
            matrix[2, 3] = pz
    else:
        matrix = np.eye(4)

    rep = getattr(envelope_product, "Representation", None)
    if rep is None:
        return None

    world_xy: list[tuple[float, float]] = []
    method_label = ""

    # --- Strategy 1: CSG → extract extrusion profile ---
    extrusion = None
    for sub_rep in rep.Representations:
        for item in sub_rep.Items:
            extrusion = _find_first_extrusion(item)
            if extrusion is not None:
                break
        if extrusion is not None:
            break

    if extrusion is not None:
        ext_matrix = matrix.copy()
        ext_pos = getattr(extrusion, "Position", None)
        if ext_pos is not None:
            loc = getattr(ext_pos, "Location", None)
            if loc is not None:
                c = getattr(loc, "Coordinates", None) or []
                pos_m = np.eye(4)
                pos_m[0, 3] = float(c[0]) if len(c) >= 1 else 0.0
                pos_m[1, 3] = float(c[1]) if len(c) >= 2 else 0.0
                pos_m[2, 3] = float(c[2]) if len(c) >= 3 else 0.0
                ext_matrix = ext_matrix @ pos_m

        profile = getattr(extrusion, "SweptArea", None)
        curve = getattr(profile, "OuterCurve", None) if profile else None
        if curve is not None and curve.is_a("IfcPolyline"):
            local_pts = []
            for pt in curve.Points:
                c = pt.Coordinates
                local_pts.append((float(c[0]) if len(c) >= 1 else 0.0,
                                  float(c[1]) if len(c) >= 2 else 0.0))
            if len(local_pts) >= 2 and local_pts[0] == local_pts[-1]:
                local_pts = local_pts[:-1]
            if len(local_pts) >= 3:
                for lx, ly in local_pts:
                    wp = ext_matrix @ np.array([lx, ly, 0.0, 1.0])
                    world_xy.append((float(wp[0]), float(wp[1])))
                method_label = "BUILDING_ENVELOPE extrusion profile"

    # --- Strategy 2: FacetedBrep → extract the lowest horizontal face ---
    if not world_xy:
        for sub_rep in rep.Representations:
            for item in sub_rep.Items:
                if not item.is_a("IfcFacetedBrep"):
                    continue
                best_face_pts: list[tuple[float, float]] = []
                best_face_z = float("inf")
                for face in item.Outer.CfsFaces:
                    for bound in face.Bounds:
                        loop = bound.Bound
                        if not hasattr(loop, "Polygon"):
                            continue
                        pts_3d = []
                        for pt in loop.Polygon:
                            c = pt.Coordinates
                            lx = float(c[0]) if len(c) >= 1 else 0.0
                            ly = float(c[1]) if len(c) >= 2 else 0.0
                            lz = float(c[2]) if len(c) >= 3 else 0.0
                            wp = matrix @ np.array([lx, ly, lz, 1.0])
                            pts_3d.append((float(wp[0]), float(wp[1]), float(wp[2])))
                        if len(pts_3d) < 3:
                            continue
                        zs = [p[2] for p in pts_3d]
                        z_range = max(zs) - min(zs)
                        avg_z = sum(zs) / len(zs)
                        # Horizontal face at or near the lowest Z
                        if z_range < 0.01 and avg_z < best_face_z:
                            best_face_z = avg_z
                            best_face_pts = [(p[0], p[1]) for p in pts_3d]
                if best_face_pts and len(best_face_pts) >= 3:
                    world_xy = best_face_pts
                    method_label = "BUILDING_ENVELOPE FacetedBrep bottom face"
                    break
            if world_xy:
                break

    if len(world_xy) < 3:
        return None

    # Compute height from the full model (all products except CADASTER_GROUND)
    all_products = list(model.by_type("IfcProduct"))
    filtered = [p for p in all_products
                if str(getattr(p, "ObjectType", "") or "").upper() not in {"CADASTER_GROUND"}]
    if not filtered:
        filtered = all_products
    all_points = _extract_world_vertices(model, filtered)

    z_min = min(p[2] for p in all_points) if all_points else 0.0
    z_max = max(p[2] for p in all_points) if all_points else 0.0
    height = max(0.0, z_max - z_min)

    xs = [p[0] for p in world_xy]
    ys = [p[1] for p in world_xy]

    return {
        "footprint_hull": world_xy,
        "min_x": min(xs),
        "min_y": min(ys),
        "max_x": max(xs),
        "max_y": max(ys),
        "z_min": z_min,
        "z_max": z_max,
        "height_m": height,
        "ground_storey": None,
        "sampled_vertices_ground": len(world_xy),
        "sampled_vertices_height": len(all_points),
        "warnings": [f"Allowed envelope footprint extracted from {method_label}."],
    }


def _projected_outline_from_triangles(
    model: ifcopenshell.file,
    products: list[Any],
    z_min: Optional[float] = None,
    z_max: Optional[float] = None,
) -> Optional[list[tuple[float, float]]]:
    """
    Build the true (possibly non-convex) XY outline of the given products by
    tessellating their geometry and unioning the projected triangles.

    Returns the exterior ring of the unioned polygon, or None when the geometry
    kernel / shapely is unavailable or no usable triangles were produced.
    """
    if _ifc_geom is None or not _HAS_SHAPELY or not products:
        return None

    settings = _ifc_geom.settings()
    try:
        settings.set(settings.USE_WORLD_COORDS, True)
    except Exception:
        try:
            settings.set("use-world-coords", True)
        except Exception:
            pass

    triangles_2d: list[_ShPolygon] = []
    for product in products:
        if getattr(product, "Representation", None) is None:
            continue
        try:
            shape = _ifc_geom.create_shape(settings, product)
        except Exception:
            continue
        try:
            geom = shape.geometry
            verts = np.asarray(geom.verts, dtype=float).reshape(-1, 3)
            faces = np.asarray(geom.faces, dtype=int).reshape(-1, 3)
        except Exception:
            continue
        if verts.size == 0 or faces.size == 0:
            continue
        for tri in faces:
            p0, p1, p2 = verts[tri[0]], verts[tri[1]], verts[tri[2]]
            if z_min is not None and z_max is not None:
                # Keep triangles whose centroid is within the requested z-band.
                cz = (p0[2] + p1[2] + p2[2]) / 3.0
                if cz < z_min or cz > z_max:
                    continue
            ring = [(float(p0[0]), float(p0[1])),
                    (float(p1[0]), float(p1[1])),
                    (float(p2[0]), float(p2[1]))]
            try:
                poly = _ShPolygon(ring)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty or poly.area < 1e-9:
                    continue
                triangles_2d.append(poly)
            except Exception:
                continue

    if not triangles_2d:
        return None

    try:
        merged = _sh_unary_union(triangles_2d)
    except Exception:
        return None

    # Pick the largest exterior ring; ignores small slivers / floating fragments.
    geoms = []
    if merged.geom_type == "Polygon":
        geoms = [merged]
    elif merged.geom_type == "MultiPolygon":
        geoms = list(merged.geoms)
    else:
        return None

    if not geoms:
        return None

    largest = max(geoms, key=lambda g: g.area)
    # Light simplification to drop tessellation noise without losing notches.
    try:
        simplified = largest.simplify(0.05, preserve_topology=True)
        if simplified.is_valid and simplified.area > 0:
            largest = simplified
    except Exception:
        pass

    coords = list(largest.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) < 3:
        return None
    return [(float(x), float(y)) for x, y in coords]


def _build_ground_perimeter_temp_volume(path: Path, exclude_object_types: set[str] | None = None) -> Dict[str, Any]:
    model = ifcopenshell.open(str(path))
    warnings: list[str] = []

    # Filter out excluded ObjectTypes (e.g. CADASTER_GROUND)
    all_products = list(model.by_type("IfcProduct"))
    if exclude_object_types:
        filtered = [p for p in all_products
                    if str(getattr(p, "ObjectType", "") or "").upper() not in exclude_object_types]
        if filtered:
            all_products = filtered

    all_points = _extract_world_vertices(model, all_products)
    if not all_points:
        raise RuntimeError(f"No geometric vertices could be extracted from IFC: {path}")

    ground_storey = _pick_ground_storey(model)
    ground_storey_name = str(getattr(ground_storey, "Name", "")) if ground_storey is not None else None

    storey_points: list[tuple[float, float, float]] = []
    if ground_storey is not None:
        storey_products = list(_iter_storey_products(ground_storey))
        if exclude_object_types and storey_products:
            storey_products = [p for p in storey_products
                               if str(getattr(p, "ObjectType", "") or "").upper() not in exclude_object_types]
        if storey_products:
            storey_points = _extract_world_vertices(model, storey_products)

    if not storey_points:
        all_min_z = min(p[2] for p in all_points)
        z_band_m = 0.60
        storey_points = [p for p in all_points if p[2] <= all_min_z + z_band_m]
        warnings.append("Ground-storey relations were not found; used lowest-z band fallback for footprint extraction.")

    if len(storey_points) < 3:
        raise RuntimeError("Ground-floor perimeter extraction failed: insufficient vertices.")

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

    # Try true (possibly non-convex) projected outline first. This captures
    # notches, recesses and L-shaped plans from the architect IFC instead of
    # over-approximating with a convex hull.
    footprint_hull: Optional[list[tuple[float, float]]] = None
    band_top = z_min + min(3.5, max(0.5, 0.4 * height))
    outline_products = main_products or all_products
    try:
        outline = _projected_outline_from_triangles(
            model, outline_products, z_min=z_min - 0.05, z_max=band_top
        )
        if outline and len(outline) >= 3:
            footprint_hull = outline
            warnings.append(
                "Footprint outline extracted via tessellated-triangle union (non-convex aware)."
            )
    except Exception as e:
        warnings.append(
            f"True outline extraction failed; falling back to convex hull. reason={type(e).__name__}: {e}"
        )

    if footprint_hull is None:
        footprint_xy = [(p[0], p[1]) for p in storey_points]
        footprint_hull = convex_hull(footprint_xy)
        warnings.append("Footprint outline approximated by convex hull (no triangulated geometry).")
    if len(footprint_hull) < 3:
        raise RuntimeError("Ground-floor perimeter extraction failed: footprint hull is degenerate.")

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


def _compute_building_base_z(path: Path) -> Optional[float]:
    """
    Lowest world-Z of *building* geometry, excluding terrain/site elements
    (IfcGeographicElement, IfcSite). Returns None on failure.

    Architect IFCs commonly bury a terrain mesh BELOW the actual ground floor,
    which makes the global bbox bottom misleading for visual alignment. This
    helper isolates the real building base so the 3D viewer can place the
    architect model on top of the cadaster slab cleanly.
    """
    if _ifc_geom is None or not path.exists():
        return None
    try:
        import multiprocessing as _mp
        model = ifcopenshell.open(str(path))
        settings = _ifc_geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        skip_types = {"IfcGeographicElement", "IfcSite"}
        try:
            workers = max(1, _mp.cpu_count() - 1)
        except Exception:
            workers = 1
        it = _ifc_geom.iterator(settings, model, workers)
        if not it.initialize():
            return None
        z_min: Optional[float] = None
        while True:
            shape = it.get()
            try:
                product = model.by_id(shape.id)
                if product is not None and product.is_a() in skip_types:
                    if not it.next():
                        break
                    continue
            except Exception:
                pass
            verts = shape.geometry.verts
            if verts:
                local_min = min(verts[2::3])
                if z_min is None or local_min < z_min:
                    z_min = local_min
            if not it.next():
                break
        return float(z_min) if z_min is not None else None
    except Exception:
        return None


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
        include_cadaster_ground=True,
    )

    if result.get("skipped"):
        raise ValueError(f"Allowed envelope could not be generated for refcat={refcat} (skipped/non-buildable).")
    if not result.get("ifc_path"):
        raise ValueError(f"Allowed envelope IFC path is missing for refcat={refcat}.")

    allowed_ifc_path = Path(result["ifc_path"]).resolve()
    architect_path = Path(architect_ifc_path).expanduser().resolve()

    allowed_bbox = _bbox_from_ifc(allowed_ifc_path)
    allowed_polygon_xy: list[tuple[float, float]] = [
        (allowed_bbox.min_x, allowed_bbox.min_y),
        (allowed_bbox.max_x, allowed_bbox.min_y),
        (allowed_bbox.max_x, allowed_bbox.max_y),
        (allowed_bbox.min_x, allowed_bbox.max_y),
    ]
    allowed_height_m = 1.0
    allowed_base_z_m = float(allowed_bbox.min_z)
    project_base_z_m = 0.0

    project_polygon_xy: Optional[list[tuple[float, float]]] = None
    intersection_polygon_xy: Optional[list[tuple[float, float]]] = None
    warnings: list[str] = []

    # Try to extract the base extrusion *profile* from the BUILDING_ENVELOPE element.
    # This avoids convex-hull contamination from hip-roof CSG clipping planes.
    try:
        profile_result = _extract_envelope_profile_footprint(allowed_ifc_path)
        if profile_result is not None:
            allowed_polygon_xy = list(profile_result["footprint_hull"])
            allowed_height_m = float(profile_result["height_m"])
            allowed_base_z_m = float(profile_result["z_min"])
            warnings.extend(profile_result.get("warnings", []))
        else:
            # Fallback: ground perimeter convex hull (no BUILDING_ENVELOPE profile found)
            allowed_temp_volume = _build_ground_perimeter_temp_volume(
                allowed_ifc_path, exclude_object_types={"CADASTER_GROUND"}
            )
            allowed_polygon_xy = list(allowed_temp_volume["footprint_hull"])
            allowed_height_m = float(allowed_temp_volume["height_m"])
            allowed_base_z_m = float(allowed_temp_volume["z_min"])
            warnings.append("Allowed envelope extracted from ground-floor perimeter (convex hull fallback).")
    except Exception as e:
        warnings.append(f"Allowed IFC footprint extraction failed; using BBox. reason={type(e).__name__}: {e}")

    # ── Helper: align polygons when they are in different coordinate systems ──
    def _centroid(poly):
        n = len(poly)
        if n == 0:
            return (0.0, 0.0)
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)

    def _translate_poly(poly, dx, dy):
        return [(p[0] + dx, p[1] + dy) for p in poly]

    def _bbox_of(poly):
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        return min(xs), min(ys), max(xs), max(ys)

    def _polygons_overlap(poly_a, poly_b, threshold=0.1):
        """Check if bounding boxes of two polygons overlap by at least threshold fraction."""
        ax0, ay0, ax1, ay1 = _bbox_of(poly_a)
        bx0, by0, bx1, by1 = _bbox_of(poly_b)
        ox = max(0, min(ax1, bx1) - max(ax0, bx0))
        oy = max(0, min(ay1, by1) - max(ay0, by0))
        aw = max(1e-9, ax1 - ax0); ah = max(1e-9, ay1 - ay0)
        overlap_ratio = (ox * oy) / (aw * ah) if (aw * ah) > 0 else 0
        return overlap_ratio > threshold

    try:
        temp_volume = _build_ground_perimeter_temp_volume(architect_path)
        project_base_z_m = float(temp_volume["z_min"])

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

        # Check if polygons already overlap (same coord system) or need alignment
        if _polygons_overlap(allowed_polygon_xy, footprint_hull):
            project_polygon_xy = list(footprint_hull)
            warnings.append("Polygons overlap naturally — same coordinate system detected.")
        else:
            # Different coordinate systems: align project centroid → allowed centroid
            ac = _centroid(allowed_polygon_xy)
            pc = _centroid(footprint_hull)
            align_dx = ac[0] - pc[0]
            align_dy = ac[1] - pc[1]
            project_polygon_xy = _translate_poly(footprint_hull, align_dx, align_dy)
            warnings.append(f"Different coordinate systems detected. Project polygon aligned to allowed centroid (offset: dx={align_dx:.2f}, dy={align_dy:.2f}).")

        project_area = _polygon_area(project_polygon_xy)
        inter_poly = polygon_intersection(project_polygon_xy, allowed_polygon_xy)
        intersection_polygon_xy = list(inter_poly) if inter_poly else None
        
        # DEBUG: Log coordinate ranges
        allowed_xs = [p[0] for p in allowed_polygon_xy]
        allowed_ys = [p[1] for p in allowed_polygon_xy]
        project_xs = [p[0] for p in project_polygon_xy]
        project_ys = [p[1] for p in project_polygon_xy]
        print(f"[COORD_DEBUG] allowed_x_range=({min(allowed_xs):.2f}, {max(allowed_xs):.2f}) "
              f"project_x_range=({min(project_xs):.2f}, {max(project_xs):.2f})")
        print(f"[COORD_DEBUG] allowed_y_range=({min(allowed_ys):.2f}, {max(allowed_ys):.2f}) "
              f"project_y_range=({min(project_ys):.2f}, {max(project_ys):.2f})")
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
        project_base_z_m = float(project_bbox.min_z)
        project_volume = project_bbox.volume()
        intersection_volume = _intersection_volume(project_bbox, allowed_bbox)
        outside_volume = max(0.0, project_volume - intersection_volume)
        raw_project_poly = [
            (project_bbox.min_x, project_bbox.min_y),
            (project_bbox.max_x, project_bbox.min_y),
            (project_bbox.max_x, project_bbox.max_y),
            (project_bbox.min_x, project_bbox.max_y),
        ]
        # Check overlap; align if needed
        if _polygons_overlap(allowed_polygon_xy, raw_project_poly):
            project_polygon_xy = raw_project_poly
        else:
            ac = _centroid(allowed_polygon_xy)
            pc = _centroid(raw_project_poly)
            project_polygon_xy = _translate_poly(raw_project_poly, ac[0] - pc[0], ac[1] - pc[1])
        inter_poly = polygon_intersection(project_polygon_xy, allowed_polygon_xy)
        intersection_polygon_xy = list(inter_poly) if inter_poly else None
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

    visual_bounds_x = [allowed_bbox.min_x, allowed_bbox.max_x, project_bbox.min_x, project_bbox.max_x]
    visual_bounds_y = [allowed_bbox.min_y, allowed_bbox.max_y, project_bbox.min_y, project_bbox.max_y]

    # Compute true building base Z for the architect IFC, excluding terrain
    # (IfcGeographicElement / IfcSite). Used by the 3D viewer to align the
    # architect model on top of the cadaster slab without being thrown off by
    # a terrain mesh that sits below the actual ground floor.
    _bbz = _compute_building_base_z(architect_path)
    architect_building_base_z = float(_bbz) if _bbz is not None else float(project_bbox.min_z)

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
        "visual_3d": {
            "allowed_base_z_m": float(allowed_base_z_m),
            "allowed_bbox_min_z_m": float(allowed_bbox.min_z),
            "allowed_base_offset_from_bbox_min_z_m": float(allowed_base_z_m - allowed_bbox.min_z),
            "architect_base_z_m": float(project_base_z_m),
            "architect_bbox_min_z_m": float(project_bbox.min_z),
            "architect_base_offset_from_bbox_min_z_m": float(project_base_z_m - project_bbox.min_z),
            "architect_building_base_z_m": float(architect_building_base_z),
            "architect_building_offset_from_bbox_min_z_m": float(architect_building_base_z - project_bbox.min_z),
        },
        "visual": {
            "view": "top_xy",
            "allowed_polygon_xy": [
                {"x": float(x), "y": float(y)} for x, y in (allowed_polygon_xy or [])
            ],
            "project_polygon_xy": [
                {"x": float(x), "y": float(y)} for x, y in (project_polygon_xy or [])
            ],
            "intersection_polygon_xy": [
                {"x": float(x), "y": float(y)} for x, y in (intersection_polygon_xy or [])
            ],
            "bounds": {
                "min_x": float(min(visual_bounds_x)),
                "max_x": float(max(visual_bounds_x)),
                "min_y": float(min(visual_bounds_y)),
                "max_y": float(max(visual_bounds_y)),
            },
        },
    }

    if not keep_allowed_ifc:
        try:
            allowed_ifc_path.unlink(missing_ok=True)
        except Exception:
            pass

    return response


# =============================================================================
# Element-level clash check
# =============================================================================

def run_element_clash_check(
    *,
    architect_ifc_path: str,
    envelope_ifc_path: str,
    tolerance_m: float = 0.01,
) -> Dict[str, Any]:
    """
    Check each IFC element in the architect model against the allowed envelope polygon.

    For each element that has geometry the function:
    - Projects its world bounding box onto the XY plane
    - Checks how much area of that bounding box falls outside the envelope polygon
    - Checks how much the element extends above the envelope maximum height
    - Reports elements where either overflow > tolerance_m

    Returns a dict with a list of clashing elements and summary counts.
    """
    if not _HAS_SHAPELY:
        raise RuntimeError("Shapely is required for element clash detection.")

    architect_path = Path(architect_ifc_path).expanduser().resolve()
    envelope_path = Path(envelope_ifc_path).expanduser().resolve()

    if not architect_path.exists():
        raise FileNotFoundError(f"Architect IFC not found: {architect_path}")
    if not envelope_path.exists():
        raise FileNotFoundError(f"Envelope IFC not found: {envelope_path}")

    warnings_out: list[str] = []

    # ------------------------------------------------------------------
    # 1. Extract allowed envelope polygon + height
    # ------------------------------------------------------------------
    allowed_polygon_xy: list[tuple[float, float]] = []
    allowed_z_min = 0.0
    allowed_height_m = 0.0

    try:
        profile_result = _extract_envelope_profile_footprint(envelope_path)
        if profile_result is not None:
            allowed_polygon_xy = list(profile_result["footprint_hull"])
            allowed_z_min = float(profile_result["z_min"])
            allowed_height_m = float(profile_result["height_m"])
            warnings_out.extend(profile_result.get("warnings", []))
        else:
            temp = _build_ground_perimeter_temp_volume(envelope_path, exclude_object_types={"CADASTER_GROUND"})
            allowed_polygon_xy = list(temp["footprint_hull"])
            allowed_z_min = float(temp["z_min"])
            allowed_height_m = float(temp["height_m"])
            warnings_out.extend(temp.get("warnings", []))
    except Exception as e:
        raise RuntimeError(f"Failed to extract envelope polygon: {type(e).__name__}: {e}")

    if len(allowed_polygon_xy) < 3:
        raise RuntimeError("Envelope polygon has fewer than 3 points.")

    allowed_shapely = _ShPolygon(allowed_polygon_xy)
    if not allowed_shapely.is_valid:
        allowed_shapely = allowed_shapely.buffer(0)

    # ------------------------------------------------------------------
    # 2. Determine XY coordinate alignment offset
    # ------------------------------------------------------------------
    align_dx, align_dy = 0.0, 0.0
    arch_z_min = 0.0

    def _centroid_local(poly: list[tuple[float, float]]) -> tuple[float, float]:
        n = len(poly)
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)

    def _bbox_of_local(poly: list[tuple[float, float]]) -> tuple[float, float, float, float]:
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        return min(xs), min(ys), max(xs), max(ys)

    def _bboxes_overlap_local(a: list[tuple[float, float]], b: list[tuple[float, float]], threshold: float = 0.1) -> bool:
        ax0, ay0, ax1, ay1 = _bbox_of_local(a)
        bx0, by0, bx1, by1 = _bbox_of_local(b)
        ox = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        oy = max(0.0, min(ay1, by1) - max(ay0, by0))
        aw = max(1e-9, ax1 - ax0) * max(1e-9, ay1 - ay0)
        return (ox * oy) / aw > threshold

    try:
        temp_vol = _build_ground_perimeter_temp_volume(architect_path)
        arch_footprint: list[tuple[float, float]] = temp_vol["footprint_hull"]
        arch_z_min = float(temp_vol["z_min"])
        warnings_out.extend(temp_vol.get("warnings", []))

        if not _bboxes_overlap_local(allowed_polygon_xy, arch_footprint):
            ac = _centroid_local(allowed_polygon_xy)
            pc = _centroid_local(arch_footprint)
            align_dx = ac[0] - pc[0]
            align_dy = ac[1] - pc[1]
            warnings_out.append(
                f"Coordinate system mismatch detected. Alignment offset applied: "
                f"dx={align_dx:.3f} m, dy={align_dy:.3f} m."
            )
    except Exception as e:
        warnings_out.append(f"Could not compute arch footprint for alignment: {type(e).__name__}: {e}")

    arch_model = ifcopenshell.open(str(architect_path))

    # When coordinate alignment was applied, the centroid shift introduces a
    # residual offset that appears identically on all elements (same side, same value).
    # Expand the envelope polygon by the alignment distance so that alignment
    # artefacts are absorbed and only genuine overflows remain.
    if abs(align_dx) > 1e-4 or abs(align_dy) > 1e-4:
        align_dist = math.hypot(align_dx, align_dy)
        try:
            expanded = allowed_shapely.buffer(align_dist)
            if expanded.is_valid and not expanded.is_empty:
                allowed_shapely = expanded
                # Also expand the bbox bounds used for per-side overflow
                ax0_e, ay0_e, ax1_e, ay1_e = _bbox_of_local(allowed_polygon_xy)
                # Store as module-level-like vars to use in loop (recomputed from polygon anyway)
                allowed_polygon_xy = list(expanded.exterior.coords[:-1])
                warnings_out.append(
                    f"Envelope expanded by {align_dist:.4f} m to absorb alignment uncertainty. "
                    f"Only overflows larger than this are reported."
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 3. Per-element check
    # ------------------------------------------------------------------
    _SKIP_TYPES = {
        "IfcSpace", "IfcGrid", "IfcSite", "IfcBuilding", "IfcBuildingStorey",
        "IfcAnnotation", "IfcOpeningElement", "IfcVirtualElement",
    }
    _SKIP_PREFIXES = ("IfcFlow", "IfcDistribution", "IfcFurnishing")

    tol = float(tolerance_m)

    clashing: list[Dict[str, Any]] = []
    total_checked = 0

    for product in arch_model.by_type("IfcProduct"):
        cls = product.is_a()
        if cls in _SKIP_TYPES:
            continue
        if any(cls.startswith(pfx) for pfx in _SKIP_PREFIXES):
            continue
        if getattr(product, "Representation", None) is None:
            continue

        pts = _extract_world_vertices(arch_model, [product])
        if not pts:
            continue

        total_checked += 1

        # Apply XY alignment
        xs = [x + align_dx for x, y, z in pts]
        ys = [y + align_dy for x, y, z in pts]
        zs = [z for x, y, z in pts]

        e_min_x, e_max_x = min(xs), max(xs)
        e_min_y, e_max_y = min(ys), max(ys)
        e_min_z, e_max_z = min(zs), max(zs)

        # XY overflow: per-side overflow is the primary check (robust against bbox inflation).
        # Area is computed secondarily for informational display.
        a_min_x, a_min_y, a_max_x, a_max_y = _bbox_of_local(allowed_polygon_xy)
        overflow_west  = max(0.0, a_min_x - e_min_x)
        overflow_east  = max(0.0, e_max_x - a_max_x)
        overflow_south = max(0.0, a_min_y - e_min_y)
        overflow_north = max(0.0, e_max_y - a_max_y)
        max_side_overflow = max(overflow_west, overflow_east, overflow_south, overflow_north)

        # Z overflow: element height vs allowed height (coordinate-system independent)
        elem_height_m = max(0.0, e_max_z - e_min_z)
        overflow_z_top_m = max(0.0, elem_height_m - allowed_height_m)

        # Clashing: at least one side OR Z exceeds tolerance
        is_clashing = (max_side_overflow > tol) or (overflow_z_top_m > tol)
        if not is_clashing:
            continue

        # Compute actual XY area outside envelope (informational)
        elem_bbox_poly = _ShPolygon([
            (e_min_x, e_min_y), (e_max_x, e_min_y),
            (e_max_x, e_max_y), (e_min_x, e_max_y),
        ])
        try:
            outside = elem_bbox_poly.difference(allowed_shapely)
            overflow_xy_m2 = float(outside.area) if not outside.is_empty else 0.0
        except Exception:
            overflow_xy_m2 = 0.0

        clashing.append({
            "id": product.id(),
            "global_id": str(getattr(product, "GlobalId", "") or ""),
            "type": cls,
            "name": str(getattr(product, "Name", "") or ""),
            "overflow_xy_m2": round(overflow_xy_m2, 4),
            "overflow_z_top_m": round(overflow_z_top_m, 4),
            "overflow_west_m": round(overflow_west, 4),
            "overflow_east_m": round(overflow_east, 4),
            "overflow_south_m": round(overflow_south, 4),
            "overflow_north_m": round(overflow_north, 4),
        })

    # Sort: largest side overflow first
    clashing.sort(key=lambda e: max(e["overflow_west_m"], e["overflow_east_m"],
                                    e["overflow_south_m"], e["overflow_north_m"]), reverse=True)

    return {
        "total_elements_checked": total_checked,
        "clashing_count": len(clashing),
        "tolerance_m": tol,
        "align_dx": round(align_dx, 4),
        "align_dy": round(align_dy, 4),
        "warnings": warnings_out,
        "elements": clashing,
    }
