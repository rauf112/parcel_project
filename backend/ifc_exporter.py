# ifc_exporter.py
"""
IFC Envelope Exporter (IFC4X3)

Purpose
-------
Builds a visualizable envelope volume in IFC from a 2D footprint polygon
(XY in meters) and zoning constraints (height, depth, roof slopes).

Geometry modes
--------------
1) No roof rule:
    - Extrudes the footprint upward by `height` along +Z (simple prism).

2) Roof rule present (hip-roof clipping):
    - `height` is interpreted as eaves height (ALTMAX).
    - The roof starts strictly ABOVE the eaves (hs_above + eps to avoid overlap).
    - Four sloped planes (hip roof) clip a tall roof base.
    - Final envelope = (walls up to eaves) UNION (roof-only above eaves).

Numerical stability
-------------------
Cadastral coordinates (UTM) can be very large. To reduce precision issues in IFC viewers:
- The footprint is localized (minX/minY -> 0,0) for geometry creation.
- The world offset is restored via the IFC ObjectPlacement.
"""

import math
from typing import List, Tuple, Optional, Dict, Any

import ifcopenshell
import ifcopenshell.guid

Point2 = Tuple[float, float]



# =============================================================================
# Geometry helpers (2D)
# =============================================================================

def _ensure_ring_open(points: List[Point2]) -> List[Point2]:
    """If polygon ring is closed (first == last), return an open ring."""
    if len(points) >= 2 and points[0] == points[-1]:
        return points[:-1]
    return points


def _to_local_xy(points: List[Point2]) -> Tuple[List[Point2], Tuple[float, float]]:
    """
    Translate footprint so that minX/minY becomes (0,0) for numeric stability.

    Returns
    -------
    local_points : List[(x,y)]
    offset       : (ox, oy)  -> original minX/minY (world offset)
    """
    pts = _ensure_ring_open(points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, miny = min(xs), min(ys)
    local = [(x - minx, y - miny) for (x, y) in pts]
    return local, (minx, miny)


def _unit(vx: float, vy: float) -> Tuple[float, float]:
    """Return unit vector of (vx,vy)."""
    L = math.hypot(vx, vy)
    if L < 1e-9:
        return (1.0, 0.0)
    return (vx / L, vy / L)


def _pick_ridge_dir_longest_edge(pts: List[Point2]) -> Tuple[float, float]:
    """
    Pick ridge direction using the longest polygon edge.
    Returns a unit vector (rx, ry).
    """
    pts = _ensure_ring_open(pts)

    best_len = -1.0
    best = (1.0, 0.0)
    n = len(pts)

    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        vx, vy = (x2 - x1), (y2 - y1)
        L = math.hypot(vx, vy)
        if L > best_len:
            best_len = L
            best = _unit(vx, vy)

    return best


def clip_polygon_by_depth(points2: List[Point2], depth_m: float) -> List[Point2]:
    """
    Clip an open-ring polygon by inward halfspace defined by the longest edge
    (front edge) and depth 'depth_m' inside the parcel.

    Returns an open ring (no repeated last point). If the clipped polygon has
    fewer than 3 vertices, returns the original polygon (safe fallback).
    """
    pts = _ensure_ring_open(points2)
    if len(pts) < 3:
        return pts

    # Compute centroid to decide inward direction
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)

    # find longest edge A->B
    best_len = -1.0
    best_idx = 0
    npts = len(pts)
    for i in range(npts):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % npts]
        L = math.hypot(x2 - x1, y2 - y1)
        if L > best_len:
            best_len = L
            best_idx = i

    A = pts[best_idx]
    B = pts[(best_idx + 1) % npts]

    # inward normal candidate (rotate edge +90deg) and flip if it points outside
    ex, ey = (B[0] - A[0], B[1] - A[1])
    n0x, n0y = (-ey, ex)
    if (n0x * (cx - A[0]) + n0y * (cy - A[1])) < 0:
        n0x, n0y = -n0x, -n0y
    nx, ny = _unit(n0x, n0y)

    # halfspace test: dot(n, p - A) <= depth_m  => v(p) = dot(n, p - A) - depth_m <= 0
    def v(p):
        return (nx * (p[0] - A[0]) + ny * (p[1] - A[1]) - float(depth_m))

    eps = 1e-9
    out = []
    for i in range(npts):
        s = pts[i]
        e = pts[(i + 1) % npts]
        vs = v(s)
        ve = v(e)
        s_in = vs <= eps
        e_in = ve <= eps

        if s_in and e_in:
            out.append(e)
        elif s_in and not e_in:
            denom = (vs - ve)
            if abs(denom) > 1e-12:
                t = vs / (vs - ve)
                ix = s[0] + t * (e[0] - s[0])
                iy = s[1] + t * (e[1] - s[1])
                out.append((ix, iy))
        elif (not s_in) and e_in:
            denom = (vs - ve)
            if abs(denom) > 1e-12:
                t = vs / (vs - ve)
                ix = s[0] + t * (e[0] - s[0])
                iy = s[1] + t * (e[1] - s[1])
                out.append((ix, iy))
            out.append(e)
        else:
            # both outside -> nothing
            pass

    # remove consecutive duplicates
    def _same(a, b, tol=1e-9):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    cleaned = []
    for p in out:
        if not cleaned or not _same(p, cleaned[-1]):
            cleaned.append(p)

    if cleaned and len(cleaned) >= 3 and _same(cleaned[0], cleaned[-1]):
        cleaned = cleaned[:-1]

    if len(cleaned) < 3:
        return pts

    return cleaned


def polygon_intersection(subject: List[Point2], clipper: List[Point2]) -> Optional[List[Point2]]:
    """
    Intersect two polygons (subject ∩ clipper) using Sutherland–Hodgman clipping
    where 'clipper' edges act as halfspaces. This is a best-effort implementation
    (works well when clipper is convex). If the result is degenerate (<3 pts),
    returns None.
    """
    subj = _ensure_ring_open(subject)
    clip = _ensure_ring_open(clipper)

    if len(subj) < 3 or len(clip) < 3:
        return None

    def area(poly: List[Point2]) -> float:
        a = 0.0
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            a += (x1 * y2 - x2 * y1)
        return 0.5 * a

    # ensure clipper is CCW for consistent inside test
    if area(clip) < 0:
        clip = list(reversed(clip))

    def cross(ax, ay, bx, by):
        return ax * by - ay * bx

    def is_inside(p: Point2, a: Point2, b: Point2) -> bool:
        return cross(b[0] - a[0], b[1] - a[1], p[0] - a[0], p[1] - a[1]) >= -1e-9

    def intersect_seg_line(s: Point2, e: Point2, a: Point2, b: Point2) -> Optional[Point2]:
        # line AB intersection with segment SE
        ax, ay = a; bx, by = b
        sx, sy = s; ex, ey = e
        ux, uy = bx - ax, by - ay
        vx, vy = ex - sx, ey - sy
        denom = cross(ux, uy, -vx, -vy)
        if abs(denom) < 1e-12:
            return None
        t = cross(ux, uy, ax - sx, ay - sy) / denom
        return (sx + t * vx, sy + t * vy)

    output = subj
    for i in range(len(clip)):
        A = clip[i]
        B = clip[(i + 1) % len(clip)]
        input_list = output
        output = []
        if not input_list:
            break
        s = input_list[-1]
        for e in input_list:
            s_in = is_inside(s, A, B)
            e_in = is_inside(e, A, B)
            if s_in and e_in:
                output.append(e)
            elif s_in and not e_in:
                ip = intersect_seg_line(s, e, A, B)
                if ip:
                    output.append(ip)
            elif not s_in and e_in:
                ip = intersect_seg_line(s, e, A, B)
                if ip:
                    output.append(ip)
                output.append(e)
            s = e

    # clean duplicates
    def _same(a, b, tol=1e-9):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    cleaned = []
    for p in output:
        if not cleaned or not _same(p, cleaned[-1]):
            cleaned.append(p)

    if len(cleaned) >= 3:
        if len(cleaned) >= 2 and _same(cleaned[0], cleaned[-1]):
            cleaned = cleaned[:-1]
        if len(cleaned) >= 3:
            return cleaned
    return None


def convex_hull(points: List[Point2]) -> List[Point2]:
    """
    Compute the convex hull of a set of 2D points using the monotone chain
    algorithm and return an open ring (no repeated last point).
    """
    pts = _ensure_ring_open(points)
    # sort unique points
    pts_sort = sorted(set(pts))
    if len(pts_sort) <= 1:
        return pts_sort

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts_sort:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts_sort):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    if len(hull) >= 3:
        return hull
    return pts


def _support_point(pts: List[Point2], ux: float, uy: float, take_max: bool) -> Point2:
    """
    Get an extreme vertex in direction u=(ux,uy).
    - take_max=True  -> argmax dot(u, p)
    - take_max=False -> argmin dot(u, p)
    """
    best_p = pts[0]
    best_v = best_p[0] * ux + best_p[1] * uy

    for (x, y) in pts[1:]:
        v = x * ux + y * uy
        if (take_max and v > best_v) or ((not take_max) and v < best_v):
            best_v = v
            best_p = (x, y)

    return best_p


# =============================================================================
# IFC low-level helpers (project/context/placement)
# =============================================================================

def _new_guid() -> str:
    return ifcopenshell.guid.new()


def _make_project_context(model: ifcopenshell.file):
    """
    Create IfcProject + Units (metre) + GeometricRepresentationContext.
    Returns (project, context, z_dir, x_dir).
    """
    project = model.create_entity("IfcProject", GlobalId=_new_guid(), Name="Envelope Project")

    length_unit = model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE", Prefix=None)
    project.UnitsInContext = model.create_entity("IfcUnitAssignment", Units=[length_unit])

    origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    z_dir = model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))
    x_dir = model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0))

    context = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextIdentifier="Model",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=model.create_entity(
            "IfcAxis2Placement3D",
            Location=origin,
            Axis=z_dir,
            RefDirection=x_dir,
        ),
    )
    project.RepresentationContexts = [context]
    return project, context, z_dir, x_dir


def _make_spatial_structure(model: ifcopenshell.file, project, zone_key: str):
    """
    Create: Site -> Building -> Storey and aggregate under project.
    Returns storey (container for the proxy element).
    """
    site = model.create_entity("IfcSite", GlobalId=_new_guid(), Name="Site", CompositionType="ELEMENT")
    building = model.create_entity(
        "IfcBuilding",
        GlobalId=_new_guid(),
        Name=f"Building Zone {zone_key}",
        CompositionType="ELEMENT",
    )
    storey = model.create_entity(
        "IfcBuildingStorey",
        GlobalId=_new_guid(),
        Name="Ground Floor",
        Elevation=0.0,
        CompositionType="ELEMENT",
    )

    model.create_entity("IfcRelAggregates", GlobalId=_new_guid(), RelatingObject=project, RelatedObjects=[site])
    model.create_entity("IfcRelAggregates", GlobalId=_new_guid(), RelatingObject=site, RelatedObjects=[building])
    model.create_entity("IfcRelAggregates", GlobalId=_new_guid(), RelatingObject=building, RelatedObjects=[storey])

    return storey


def _place_proxy_at_offset(model: ifcopenshell.file, proxy, ox: float, oy: float, oz: float, z_dir, x_dir):
    """
    Place proxy at world offset (ox,oy,oz). Geometry itself is local (small coords).
    """
    proxy.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=None,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(float(ox), float(oy), float(oz))),
            Axis=z_dir,
            RefDirection=x_dir,
        ),
    )



def _attach_proxy_to_storey(model: ifcopenshell.file, storey, proxy):
    """Contain proxy in storey."""
    model.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=_new_guid(),
        RelatingStructure=storey,
        RelatedElements=[proxy],
    )


def _add_pset_roof_slopes(model, element, real_deg: Optional[float], virtual_deg: Optional[float]):
    """
    Store roof slope constraints as custom properties (debug/inspection).
    """
    props = []

    if real_deg is not None:
        props.append(
            model.create_entity(
                "IfcPropertySingleValue",
                Name="MaxRoofSlopeRealDeg",
                NominalValue=model.create_entity("IfcReal", float(real_deg)),
                Unit=None,
            )
        )

    if virtual_deg is not None:
        props.append(
            model.create_entity(
                "IfcPropertySingleValue",
                Name="MaxRoofSlopeVirtualDeg",
                NominalValue=model.create_entity("IfcReal", float(virtual_deg)),
                Unit=None,
            )
        )

    if not props:
        return

    pset = model.create_entity(
        "IfcPropertySet",
        GlobalId=_new_guid(),
        Name="Pset_ZoningRoofConstraints",
        HasProperties=props,
    )

    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_new_guid(),
        RelatedObjects=[element],
        RelatingPropertyDefinition=pset,
    )


def _add_pset_street_metrics(model, element, street_metrics: Optional[Dict[str, Any]]):
    if not street_metrics or not isinstance(street_metrics, dict):
        return

    props = []

    source = street_metrics.get("source")
    if source is not None:
        props.append(
            model.create_entity(
                "IfcPropertySingleValue",
                Name="Source",
                NominalValue=model.create_entity("IfcLabel", str(source)),
                Unit=None,
            )
        )

    for name, key in [
        ("SegmentCount", "segment_count"),
        ("StreetSegmentCount", "street_segment_count"),
        ("StreetMinM", "street_min_m"),
        ("StreetMaxM", "street_max_m"),
        ("StreetAvgM", "street_avg_m"),
    ]:
        if key not in street_metrics:
            continue
        try:
            value = float(street_metrics.get(key))
        except Exception:
            continue
        props.append(
            model.create_entity(
                "IfcPropertySingleValue",
                Name=name,
                NominalValue=model.create_entity("IfcReal", value),
                Unit=None,
            )
        )

    if not props:
        return

    pset = model.create_entity(
        "IfcPropertySet",
        GlobalId=_new_guid(),
        Name="Pset_PreprocessStreetMetrics",
        HasProperties=props,
    )

    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_new_guid(),
        RelatedObjects=[element],
        RelatingPropertyDefinition=pset,
    )


# =============================================================================
# IFC profile + solids
# =============================================================================

def _make_closed_profile(model: ifcopenshell.file, pts2: List[Point2]):
    """
    Build IfcArbitraryClosedProfileDef from an open ring (pts2).
    """
    pts2 = _ensure_ring_open(pts2)

    cartesian_points = [
        model.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y), 0.0))
        for (x, y) in pts2
    ]

    # Ensure closure (repeat first point)
    x0, y0 = pts2[0]
    cartesian_points.append(model.create_entity("IfcCartesianPoint", Coordinates=(float(x0), float(y0), 0.0)))

    polyline = model.create_entity("IfcPolyline", Points=cartesian_points)

    return model.create_entity("IfcArbitraryClosedProfileDef", ProfileType="AREA", OuterCurve=polyline)


def _make_extruded_solid(model: ifcopenshell.file, profile, depth: float):
    """
    Create IfcExtrudedAreaSolid along +Z.
    """
    extrusion_direction = model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))
    axis_placement = model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        Axis=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)),
    )

    return model.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=axis_placement,
        ExtrudedDirection=extrusion_direction,
        Depth=float(depth),
    )


def _make_ifc_plane(model, origin_xyz, normal_xyz):
    """
    Create an IfcPlane defined by Axis2Placement3D.
    Axis direction = plane normal.
    """
    ox, oy, oz = origin_xyz
    nx, ny, nz = normal_xyz

    return model.create_entity(
        "IfcPlane",
        Position=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(float(ox), float(oy), float(oz))),
            Axis=model.create_entity("IfcDirection", DirectionRatios=(float(nx), float(ny), float(nz))),
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)),
        ),
    )


def _make_halfspace(model, plane, keep_side: str):
    """
    Create IfcHalfSpaceSolid.
    Note: AgreementFlag can be interpreted differently by some IFC viewers.

    keep_side:
        - "below": keep volume below the roof plane (envelope behavior)
        - "above": keep volume above the plane
    """
    agreement = True if keep_side == "below" else False
    return model.create_entity("IfcHalfSpaceSolid", BaseSurface=plane, AgreementFlag=agreement)


def _horizontal_halfspace(model, z: float, keep: str):
    """
    Horizontal plane at height z. Used to force roof to start ABOVE eaves.
    keep="above" -> keep z >= threshold (AgreementFlag may be inverted by some viewers)
    """
    plane = _make_ifc_plane(model, origin_xyz=(0.0, 0.0, z), normal_xyz=(0.0, 0.0, 1.0))
    agreement = False if keep == "above" else True
    return model.create_entity("IfcHalfSpaceSolid", BaseSurface=plane, AgreementFlag=agreement)


def _bool(model, op: str, a, b):
    """Create an IfcBooleanResult."""
    return model.create_entity("IfcBooleanResult", Operator=op, FirstOperand=a, SecondOperand=b)

def _assign_layer(model, items, layer_name: str):
    """
    Assigns a presentation layer to representation items.
    Works for items such as IfcBooleanResult / IfcBooleanClippingResult as well.
    """
    model.create_entity(
        "IfcPresentationLayerAssignment",
        Name=layer_name,
        AssignedItems=list(items)  # <-- critical: items are linked here
    )

def create_ground_volume(
    model: ifcopenshell.file,
    context,
    storey,
    pts2_local,
    ox: float,
    oy: float,
    z_dir,
    x_dir,
    ground_height: float = 1.0,
    layer_name: str = "CADASTER_GROUND",
    name: str = "CadasterGround",
):
    ground_proxy = model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId=_new_guid(),
        Name=name,
        ObjectType="CADASTER_GROUND",
    )

    # Ground placement: Z = -1.0 (bottom of ground slab), height = 1.0 -> top at Z = 0.0
    _place_proxy_at_offset(model, ground_proxy, ox, oy, -1.0, z_dir, x_dir)
    _attach_proxy_to_storey(model, storey, ground_proxy)

    profile = _make_closed_profile(model, pts2_local)
    solid = _make_extruded_solid(model, profile, depth=float(ground_height))

    shape_rep = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=[solid],
    )

    # Layer assignment must target representation items
    _assign_layer(model, shape_rep.Items, layer_name)

    ground_proxy.Representation = model.create_entity(
        "IfcProductDefinitionShape",
        Representations=[shape_rep],
    )

    return ground_proxy


def _style_item(model, item, rgb=(0.2, 0.6, 1.0), transparency=0.75, name="Style"):
    """
    IFC4X3 uyumlu stil bağlama:
    IfcStyledItem -> Styles: [IfcSurfaceStyle]
    """
    r, g, b = rgb

    colour = model.create_entity(
        "IfcColourRgb",
        Name=None,
        Red=float(r),
        Green=float(g),
        Blue=float(b)
    )

    rendering = model.create_entity(
        "IfcSurfaceStyleRendering",
        SurfaceColour=colour,
        Transparency=float(transparency),
        ReflectanceMethod="NOTDEFINED"
    )

    surface_style = model.create_entity(
        "IfcSurfaceStyle",
        Name=name,
        Side="BOTH",
        Styles=[rendering]
    )

    model.create_entity(
        "IfcStyledItem",
        Item=item,
        Styles=[surface_style],   # <-- kritik: doğrudan surface_style
        Name=name
    )






def _clip_intersections(model, base, halfspaces):
    """
    Successive INTERSECTION clipping (BooleanClippingResult chain).
    """
    clipped = base
    for hs in halfspaces:
        clipped = model.create_entity(
            "IfcBooleanClippingResult",
            Operator="INTERSECTION",
            FirstOperand=clipped,
            SecondOperand=hs,
        )
    return clipped


# =============================================================================
# Roof logic (HIP roof)
# =============================================================================

def _hip_roof_halfspaces(model, pts2: List[Point2], h_eaves: float, slope_deg: float, max_rise_m: Optional[float] = None):
    """
    Build 4 roof planes for a hip roof:
    - 2 planes along ridge-perpendicular axis (S)
    - 2 planes along ridge axis (R)
    Planes are anchored at actual polygon support points (not bbox).

    Optional `max_rise_m` caps the computed rise to avoid very tall narrow wedges
    on pathological parcels.
    """
    pts2 = _ensure_ring_open(pts2)
    t = math.tan(math.radians(slope_deg))

    # Ridge direction from real footprint
    rx, ry = _pick_ridge_dir_longest_edge(pts2)
    # Perpendicular
    sx, sy = (-ry, rx)

    # Compute spans using projections (true footprint, not bbox)
    proj_r = [x * rx + y * ry for (x, y) in pts2]
    proj_s = [x * sx + y * sy for (x, y) in pts2]
    span_r = max(proj_r) - min(proj_r)
    span_s = max(proj_s) - min(proj_s)

    half_r = 0.5 * span_r
    half_s = 0.5 * span_s

    # Max rise (for roof_base height)
    rise_max = max(half_r, half_s) * t

    # Apply cap if requested
    if max_rise_m is not None and rise_max > float(max_rise_m):
        orig = rise_max
        rise_max = float(max_rise_m)
        print(f"[ROOF] rise_max capped from {orig:.3f} to {rise_max:.3f}")

    planes = []

    # +S side
    p = _support_point(pts2, sx, sy, take_max=True)
    n = (t * sx, t * sy, 1.0)
    planes.append(_make_halfspace(model, _make_ifc_plane(model, (p[0], p[1], h_eaves), n), "below"))

    # -S side
    p = _support_point(pts2, sx, sy, take_max=False)
    n = (-t * sx, -t * sy, 1.0)
    planes.append(_make_halfspace(model, _make_ifc_plane(model, (p[0], p[1], h_eaves), n), "below"))

    # +R side
    p = _support_point(pts2, rx, ry, take_max=True)
    n = (t * rx, t * ry, 1.0)
    planes.append(_make_halfspace(model, _make_ifc_plane(model, (p[0], p[1], h_eaves), n), "below"))

    # -R side
    p = _support_point(pts2, rx, ry, take_max=False)
    n = (-t * rx, -t * ry, 1.0)
    planes.append(_make_halfspace(model, _make_ifc_plane(model, (p[0], p[1], h_eaves), n), "below"))

    return planes, rise_max


# =============================================================================
# Public API
# =============================================================================

def create_ifc_envelope(
    footprint_points: List[Point2],
    height: float,
    zone_key: str,
    out_path: str,
    roof_slope_deg_real: Optional[float] = None,
    roof_slope_deg_virtual: Optional[float] = None,
    ground_height: float = 1.0,
    depth_m: Optional[float] = None,
    max_roof_rise_m: Optional[float] = None,
    ground_footprint_points=None,
    street_metrics: Optional[Dict[str, Any]] = None,
):
    """
    Create one IFC representing the parcel envelope.

    Parameters
    ----------
    footprint_points:
        Polygon footprint XY points in meters (can be global coords).
    height:
        If roof_slope_deg_real is None -> max height of prism.
        Else -> eaves height (ALTMAX at boundary).
    roof_slope_deg_real:
        If provided -> hip-roof clipping envelope.
        If None -> simple extrusion.
    roof_slope_deg_virtual:
        Stored only as metadata for now.
    """
    model = ifcopenshell.file(schema="IFC4X3")

    # Project + context
    project, context, z_dir, x_dir = _make_project_context(model)
    storey = _make_spatial_structure(model, project, zone_key)

    # Envelope element (proxy)
    proxy = model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId=_new_guid(),
        Name=f"Envelope_{zone_key}",
        ObjectType="BUILDING_ENVELOPE",
    )

    # Localize footprint and place proxy back at world offset
    # Localize ENVELOPE footprint (for envelope + roof math)
    env_pts2_local, (ox, oy) = _to_local_xy(footprint_points)

    # Localize GROUND footprint (defaults to envelope footprint if not provided)
    ground_fp = ground_footprint_points or footprint_points
    ground_pts2_local, (gox, goy) = _to_local_xy(ground_fp)

    # 1) Cadastre ground (always create, parcel footprint, fixed -1..0 Z range)
    create_ground_volume(
        model=model,
        context=context,
        storey=storey,
        pts2_local=ground_pts2_local,
        ox=gox, oy=goy,
        z_dir=z_dir, x_dir=x_dir,
        ground_height=float(ground_height),
        layer_name="CADASTER_GROUND",
    )

    # Use envelope footprint from here on
    pts2_local = env_pts2_local

    # Buildable footprint: clip by maximum depth (PROFEDIF) if provided.
    if depth_m is not None and depth_m > 0.0:
        pts2_buildable = clip_polygon_by_depth(pts2_local, float(depth_m))
    else:
        pts2_buildable = pts2_local

    # Debug: report depth clipping outcome
    if depth_m is not None:
        try:
            cx = sum(x for x, y in pts2_local) / len(pts2_local)
            cy = sum(y for x, y in pts2_local) / len(pts2_local)
            best_len = -1; best_idx = 0
            for i in range(len(pts2_local)):
                x1, y1 = pts2_local[i]
                x2, y2 = pts2_local[(i + 1) % len(pts2_local)]
                L = math.hypot(x2 - x1, y2 - y1)
                if L > best_len:
                    best_len = L; best_idx = i
            A = pts2_local[best_idx]; B = pts2_local[(best_idx + 1) % len(pts2_local)]
            n0x, n0y = (-(B[1] - A[1]), (B[0] - A[0]))
            if (n0x * (cx - A[0]) + n0y * (cy - A[1])) < 0:
                n0x, n0y = -n0x, -n0y
            nx, ny = _unit(n0x, n0y)
            max_proj = max(nx * (p[0] - A[0]) + ny * (p[1] - A[1]) for p in pts2_local)
            print(f"[DEPTH] depth_m={depth_m}, max_proj={max_proj:.3f}, orig_pts={len(pts2_local)}, buildable_pts={len(pts2_buildable)}")
        except Exception as e:
            print(f"[DEPTH] debug failed: {e}")

    # 2) Envelope starts at legal ground level (Z = 0.0)
    _place_proxy_at_offset(model, proxy, ox, oy, 0.0, z_dir, x_dir) 

    # Attach constraints (optional metadata)
    _add_pset_roof_slopes(model, proxy, roof_slope_deg_real, roof_slope_deg_virtual)
    _add_pset_street_metrics(model, proxy, street_metrics)

    # Contain in storey
    _attach_proxy_to_storey(model, storey, proxy)

    # 2D profile in local coords (buildable footprint for envelope/roof)
    profile = _make_closed_profile(model, pts2_buildable)
    h_eaves = float(height)

    # -------------------------------------------------------------------------
    # Geometry build
    # -------------------------------------------------------------------------
    if roof_slope_deg_real is None:
        # Simple prism up to 'height'
        solid = _make_extruded_solid(model, profile, depth=float(height))
        shape_rep = model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=context,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=[solid],
        )

    else:
        # Walls up to eaves (ALTMAX)
        walls_solid = _make_extruded_solid(model, profile, depth=h_eaves)

        # Build a tall roof base, clip by hip planes
        halfspaces_roof, rise_max = _hip_roof_halfspaces(model, pts2_buildable, h_eaves, float(roof_slope_deg_real), max_rise_m=max_roof_rise_m)
        big_height = h_eaves + rise_max + 1.0
        roof_base = _make_extruded_solid(model, profile, depth=big_height)

        # Roof wedge (hip planes intersection)
        roof_wedge = _clip_intersections(model, roof_base, halfspaces_roof)

        # Force roof to start ABOVE eaves (avoid coplanar overlap / z-fighting)
        eps = 0.001  # 1mm
        hs_above = _horizontal_halfspace(model, h_eaves + eps, keep="above")
        roof_wedge = _clip_intersections(model, roof_wedge, [hs_above])

        # Keep only roof part above eaves (subtract walls volume)
        roof_only = _bool(model, "DIFFERENCE", roof_wedge, walls_solid)

        # Final envelope
        envelope_solid = _bool(model, "UNION", walls_solid, roof_only)

        shape_rep = model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=context,
            RepresentationIdentifier="Body",
            RepresentationType="CSG",
            Items=[envelope_solid],
        )
        # Assign layer after shape_rep is created
        _assign_layer(model, shape_rep.Items, "REAL_ENVELOPE")


    # Assign shape
    proxy.Representation = model.create_entity(
        "IfcProductDefinitionShape",
        Representations=[shape_rep],
    )
    # -------------------------------------------------------------------------
    # OPTIONAL: Virtual roof as a separate element/layer
    # -------------------------------------------------------------------------
    if roof_slope_deg_virtual is not None and roof_slope_deg_real is not None:
        virtual_proxy = model.create_entity(
            "IfcBuildingElementProxy",
            GlobalId=_new_guid(),
            Name=f"VirtualRoof_{zone_key}",
            ObjectType="VIRTUAL_ROOF",
        )

        # Same placement offset (so it sits exactly on the footprint)
        _place_proxy_at_offset(model, virtual_proxy, ox, oy, 0.0, z_dir, x_dir)
        _attach_proxy_to_storey(model, storey, virtual_proxy)

        # Optional: also store both constraints on the virtual element
        _add_pset_roof_slopes(model, virtual_proxy, roof_slope_deg_real, roof_slope_deg_virtual)

        # Build virtual roof wedge with virtual slope
        hs_virtual, rise_v = _hip_roof_halfspaces(model, pts2_buildable, h_eaves, float(roof_slope_deg_virtual), max_rise_m=max_roof_rise_m)
        big_h_v = h_eaves + rise_v + 1.0
        roof_base_v = _make_extruded_solid(model, profile, depth=big_h_v)

        roof_wedge_v = _clip_intersections(model, roof_base_v, hs_virtual)

        # Start strictly above eaves (avoid overlap)
        eps = 0.001
        hs_above_v = _horizontal_halfspace(model, h_eaves + eps, keep="above")
        roof_wedge_v = _clip_intersections(model, roof_wedge_v, [hs_above_v])

        # Keep only roof (remove any part that coincides with walls)
        walls_solid_v = _make_extruded_solid(model, profile, depth=h_eaves)
        roof_only_v = _bool(model, "DIFFERENCE", roof_wedge_v, walls_solid_v)


        virtual_shape_rep = model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=context,
            RepresentationIdentifier="Body",
            RepresentationType="CSG",
            Items=[roof_only_v],
        )

        # Put virtual roof on its own layer
        _assign_layer(model, virtual_shape_rep.Items, "VIRTUAL_ROOF")

        # Transparency + color (virtual roof)
        for it in virtual_shape_rep.Items:
            _style_item(model, it, rgb=(0.2, 0.6, 1.0), transparency=0.75, name="VirtualRoofStyle")

        virtual_proxy.Representation = model.create_entity(
            "IfcProductDefinitionShape",
            Representations=[virtual_shape_rep],
        )


    # Write IFC
    model.write(out_path)
