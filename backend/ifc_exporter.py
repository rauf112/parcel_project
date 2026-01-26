# ifc_exporter.py
"""
IFC Envelope Exporter (IFC4X3)

Goal:
- Given a 2D footprint polygon (XY points in meters) and zoning constraints,
  create an IFC "envelope volume" for visualization / checking.

Supported geometry modes:
1) Simple extrude up to ALTMAX (if no roof slope rule is used)
2) Clip-based gable roof envelope:
   - ALTMAX is interpreted as "eaves height" (height at the boundary)
   - roof is allowed to go ABOVE ALTMAX, but NOT exceed the max roof slope
   - implemented using boolean clipping (IfcHalfSpaceSolid + IfcBooleanClippingResult)

Important detail:
- Many cadastral points are in large coordinate systems (UTM etc.)
  Large coordinates can cause floating point precision issues in IFC viewers.
  We solve this by:
    a) translating the footprint to local coordinates (small numbers)
    b) setting ObjectPlacement of the IFC element back to the original offset
"""

import math
from typing import List, Tuple, Optional

import ifcopenshell
import ifcopenshell.guid

Point2 = Tuple[float, float]


# =============================================================================
# Small geometry helpers
# =============================================================================

def _ensure_ring_open(points: List[Point2]) -> List[Point2]:
    """If the polygon ring is closed (first==last), return an open ring."""
    if len(points) >= 2 and points[0] == points[-1]:
        return points[:-1]
    return points


def _bbox(points: List[Point2]) -> Tuple[float, float, float, float]:
    """Axis-aligned bounding box of 2D points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _to_local_xy(points: List[Point2]) -> Tuple[List[Point2], Tuple[float, float]]:
    """
    Translate points so that minX/minY becomes (0,0).
    Returns: (local_points, (offset_x, offset_y))
    """
    pts = _ensure_ring_open(points)
    minx, miny, _, _ = _bbox(pts)
    local = [(x - minx, y - miny) for (x, y) in pts]
    return local, (minx, miny)


def _pick_ridge_dir(points: List[Point2]) -> str:
    """
    Decide ridge direction for a gable roof:
    - If footprint bbox is longer in X -> ridge along X
    - else ridge along Y
    This is a heuristic. Later we can replace it with "longest edge" or PCA.
    """
    minx, miny, maxx, maxy = _bbox(points)
    dx = maxx - minx
    dy = maxy - miny
    return "x" if dx >= dy else "y"


# =============================================================================
# IFC low-level helpers (units, context, placements)
# =============================================================================

def _new_guid() -> str:
    return ifcopenshell.guid.new()


def _make_project_context(model: ifcopenshell.file):
    """
    Create IFC Project + Units + Geometric Representation Context.
    Returns: (project, context, z_dir, x_dir)
    """
    project = model.create_entity(
        "IfcProject",
        GlobalId=_new_guid(),
        Name="Envelope Project"
    )

    length_unit = model.create_entity(
        "IfcSIUnit",
        UnitType="LENGTHUNIT",
        Name="METRE",
        Prefix=None
    )
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
            RefDirection=x_dir
        )
    )
    project.RepresentationContexts = [context]
    return project, context, z_dir, x_dir


def _make_spatial_structure(model: ifcopenshell.file, project, zone_key: str):
    """
    Create: Site -> Building -> Storey and aggregate them under the project.
    Returns: storey (where we'll contain our envelope proxy)
    """
    site = model.create_entity(
        "IfcSite",
        GlobalId=_new_guid(),
        Name="Site",
        CompositionType="ELEMENT"
    )

    building = model.create_entity(
        "IfcBuilding",
        GlobalId=_new_guid(),
        Name=f"Building Zone {zone_key}",
        CompositionType="ELEMENT"
    )

    storey = model.create_entity(
        "IfcBuildingStorey",
        GlobalId=_new_guid(),
        Name="Ground Floor",
        Elevation=0.0,
        CompositionType="ELEMENT"
    )

    model.create_entity("IfcRelAggregates", GlobalId=_new_guid(), RelatingObject=project, RelatedObjects=[site])
    model.create_entity("IfcRelAggregates", GlobalId=_new_guid(), RelatingObject=site, RelatedObjects=[building])
    model.create_entity("IfcRelAggregates", GlobalId=_new_guid(), RelatingObject=building, RelatedObjects=[storey])

    return storey


def _place_proxy_at_offset(model: ifcopenshell.file, proxy, ox: float, oy: float, z_dir, x_dir):
    """
    Place the proxy at world coordinate (ox, oy, 0), while its geometry is defined locally (0..N).
    This avoids large-number precision issues.
    """
    proxy.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=None,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(float(ox), float(oy), 0.0)),
            Axis=z_dir,
            RefDirection=x_dir
        )
    )


def _attach_proxy_to_storey(model: ifcopenshell.file, storey, proxy):
    model.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=_new_guid(),
        RelatingStructure=storey,
        RelatedElements=[proxy]
    )


def _add_pset_roof_slopes(model, element, real_deg: Optional[float], virtual_deg: Optional[float]):
    """
    Store the slope constraints as custom properties on the proxy.
    This is useful for debugging / inspection later.
    """
    props = []

    if real_deg is not None:
        props.append(model.create_entity(
            "IfcPropertySingleValue",
            Name="MaxRoofSlopeRealDeg",
            NominalValue=model.create_entity("IfcReal", float(real_deg)),
            Unit=None
        ))

    if virtual_deg is not None:
        props.append(model.create_entity(
            "IfcPropertySingleValue",
            Name="MaxRoofSlopeVirtualDeg",
            NominalValue=model.create_entity("IfcReal", float(virtual_deg)),
            Unit=None
        ))

    if not props:
        return

    pset = model.create_entity(
        "IfcPropertySet",
        GlobalId=_new_guid(),
        Name="Pset_ZoningRoofConstraints",
        HasProperties=props
    )

    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_new_guid(),
        RelatedObjects=[element],
        RelatingPropertyDefinition=pset
    )


# =============================================================================
# IFC profile + solids
# =============================================================================

def _make_closed_profile(model: ifcopenshell.file, pts2: List[Point2]):
    """
    Make IfcArbitraryClosedProfileDef from an open ring (pts2).
    """
    pts2 = _ensure_ring_open(pts2)

    cartesian_points = [
        model.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y), 0.0))
        for (x, y) in pts2
    ]

    # Close polyline explicitly
    first = pts2[0]
    last = pts2[-1]
    if first != last:
        cartesian_points.append(
            model.create_entity("IfcCartesianPoint", Coordinates=(float(first[0]), float(first[1]), 0.0))
        )

    polyline = model.create_entity("IfcPolyline", Points=cartesian_points)

    return model.create_entity(
        "IfcArbitraryClosedProfileDef",
        ProfileType="AREA",
        OuterCurve=polyline
    )


def _make_extruded_solid(model: ifcopenshell.file, profile, depth: float):
    """
    Create a simple IfcExtrudedAreaSolid along +Z.
    """
    extrusion_direction = model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))

    axis_placement = model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        Axis=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0))
    )

    return model.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=axis_placement,
        ExtrudedDirection=extrusion_direction,
        Depth=float(depth)
    )


# =============================================================================
# Clip-based gable roof (Halfspaces + BooleanClippingResult)
# =============================================================================

def _make_ifc_plane(model, origin_xyz, normal_xyz):
    """
    Create an IfcPlane with a given origin point and normal direction.
    In IFC, IfcPlane is defined by an IfcAxis2Placement3D.
    The 'Axis' direction is the plane normal.
    """
    ox, oy, oz = origin_xyz
    nx, ny, nz = normal_xyz

    return model.create_entity(
        "IfcPlane",
        Position=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(float(ox), float(oy), float(oz))),
            Axis=model.create_entity("IfcDirection", DirectionRatios=(float(nx), float(ny), float(nz))),
            # RefDirection can be any non-parallel direction; (1,0,0) is OK in our local coords
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)),
        )
    )


def _make_halfspace(model, plane, keep_side: str):
    """
    IfcHalfSpaceSolid keeps one side of a plane.
    AgreementFlag interpretation can be confusing and viewer-dependent.
    We keep a simple switch here so you can flip if needed.

    keep_side:
      - "below": we want to keep the volume under the roof plane (typical envelope)
      - "above": keep volume above the plane
    """
    if keep_side == "below":
        agreement = False
    else:
        agreement = True
    return model.create_entity("IfcHalfSpaceSolid", BaseSurface=plane, AgreementFlag=agreement)


def _gable_roof_halfspaces(model, pts2: List[Point2], h_eaves: float, slope_deg: float):
    """
    Build two halfspaces that represent a gable roof limit.

    Assumption:
    - ALTMAX = eaves height (height at parcel boundary)
    - roof may go ABOVE ALTMAX but must respect slope_deg

    Returns:
      (halfspaces, rise_max)
        - halfspaces: [IfcHalfSpaceSolid, IfcHalfSpaceSolid]
        - rise_max: maximum possible roof rise based on bbox span and slope
    """
    minx, miny, maxx, maxy = _bbox(pts2)
    t = math.tan(math.radians(slope_deg))
    ridge_dir = _pick_ridge_dir(pts2)

    halfspaces = []

    if ridge_dir == "x":
        # ridge along X, slope across Y
        # plane through (y=miny, z=h_eaves) rising with slope towards center
        plane1 = _make_ifc_plane(model, origin_xyz=(0.0, miny, h_eaves), normal_xyz=(0.0, -t, 1.0))
        halfspaces.append(_make_halfspace(model, plane1, keep_side="below"))

        # plane through (y=maxy, z=h_eaves) rising towards center from the other side
        plane2 = _make_ifc_plane(model, origin_xyz=(0.0, maxy, h_eaves), normal_xyz=(0.0, t, 1.0))
        halfspaces.append(_make_halfspace(model, plane2, keep_side="below"))

        rise_max = (maxy - miny) * 0.5 * t

    else:
        # ridge along Y, slope across X
        plane1 = _make_ifc_plane(model, origin_xyz=(minx, 0.0, h_eaves), normal_xyz=(-t, 0.0, 1.0))
        halfspaces.append(_make_halfspace(model, plane1, keep_side="below"))

        plane2 = _make_ifc_plane(model, origin_xyz=(maxx, 0.0, h_eaves), normal_xyz=(t, 0.0, 1.0))
        halfspaces.append(_make_halfspace(model, plane2, keep_side="below"))

        rise_max = (maxx - minx) * 0.5 * t

    return halfspaces, rise_max


def _apply_boolean_clipping(model, base_solid, halfspaces):
    """
    Apply successive clipping operations.
    We use DIFFERENCE(base, halfspace) to cut away the "forbidden" side.
    If you see inverted results in the viewer, it's usually because the halfspace side is flipped.
    """
    clipped = base_solid
    for hs in halfspaces:
        clipped = model.create_entity(
            "IfcBooleanClippingResult",
            Operator="DIFFERENCE",
            FirstOperand=clipped,
            SecondOperand=hs
        )
    return clipped


# =============================================================================
# Public API
# =============================================================================

def create_ifc_envelope(
    footprint_points: List[Point2],
    height: float,
    zone_key: str,
    out_path: str,
    roof_slope_deg_real: Optional[float] = None,
    roof_slope_deg_virtual: Optional[float] = None
):
    """
    Create one IFC file representing the parcel envelope.

    Parameters
    ----------
    footprint_points:
        Polygon footprint XY points in meters (can be global coords).
    height:
        Interpreted as:
          - If roof slope is None: max height of the volume
          - If roof slope is given: "eaves height" (ALTMAX at boundary)
    roof_slope_deg_real:
        If provided -> use clip-based gable roof envelope.
        If None -> simple extrude.
    roof_slope_deg_virtual:
        Currently stored as metadata only (pset). (You can later use it for a second envelope layer.)
    """
    model = ifcopenshell.file(schema="IFC4X3")

    # 1) Project/context setup
    project, context, z_dir, x_dir = _make_project_context(model)
    storey = _make_spatial_structure(model, project, zone_key)

    # 2) Create envelope proxy element
    proxy = model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId=_new_guid(),
        Name=f"Envelope_{zone_key}",
        ObjectType="BUILDING_ENVELOPE"
    )

    # 3) Localize footprint for numerical stability + place back into world coords
    pts2_local, (ox, oy) = _to_local_xy(footprint_points)
    _place_proxy_at_offset(model, proxy, ox, oy, z_dir, x_dir)

    # 4) Attach metadata (roof slopes)
    _add_pset_roof_slopes(model, proxy, roof_slope_deg_real, roof_slope_deg_virtual)

    # 5) Put proxy into spatial structure
    _attach_proxy_to_storey(model, storey, proxy)

    # 6) Build a closed 2D profile (in LOCAL coords)
    profile = _make_closed_profile(model, pts2_local)

    # 7) Geometry path selection
    if roof_slope_deg_real is None:
        # --- Simple extrude up to 'height' ---
        solid = _make_extruded_solid(model, profile, depth=float(height))

        shape_rep = model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=context,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=[solid]
        )
    else:
        # --- Clip-based roof envelope ---
        h_eaves = float(height)  # ALTMAX as eaves height
        halfspaces, rise_max = _gable_roof_halfspaces(model, pts2_local, h_eaves, float(roof_slope_deg_real))

        # We extrude higher than eaves so that the roof can rise above ALTMAX
        big_height = h_eaves + rise_max + 1.0  # +1m safety
        base_solid = _make_extruded_solid(model, profile, depth=float(big_height))

        clipped = _apply_boolean_clipping(model, base_solid, halfspaces)

        shape_rep = model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=context,
            RepresentationIdentifier="Body",
            RepresentationType="CSG",
            Items=[clipped]
        )

    # 8) Assign the shape to the proxy
    proxy.Representation = model.create_entity(
        "IfcProductDefinitionShape",
        Representations=[shape_rep]
    )

    # 9) Write IFC
    model.write(out_path)
