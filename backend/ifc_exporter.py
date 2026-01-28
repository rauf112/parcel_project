# ifc_exporter.py
"""
IFC Envelope Exporter (IFC4X3)

Amaç
-----
2D footprint poligonundan (XY, metre) ve zoning kısıtlarından,
IFC içinde görselleştirilebilir bir "envelope volume" üretmek.

Geometri Modları
----------------
1) Roof kuralı yoksa:
   - footprint'i 'height' kadar +Z yönünde extrude eder (basit prizma)

2) Roof kuralı varsa (hip roof clipping):
   - 'height' = ALTMAX = saçak kotu (eaves height)
   - Çatı, bu kotun ÜSTÜNDE başlar (hs_above + eps ile garanti)
   - Roof slope (deg) ile sınırlandırılmış 4 eğimli düzlemle (hip) clip edilir
   - Son envelope = (walls up to eaves) UNION (roof-only above eaves)

Numerik Stabilite
-----------------
Kadastral koordinatlar (UTM vb.) çok büyük olabilir. Viewer'larda precision sorunları çıkmasın diye:
- footprint'i local'e taşırız (minX/minY -> 0,0)
- IFC element'in ObjectPlacement'ına world offset'i geri koyarız
"""

import math
from typing import List, Tuple, Optional

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


def _place_proxy_at_offset(model: ifcopenshell.file, proxy, ox: float, oy: float, z_dir, x_dir):
    """
    Place proxy at world offset (ox,oy,0). Geometry itself is local (small coords).
    """
    proxy.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=None,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(float(ox), float(oy), 0.0)),
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
    AgreementFlag viewer'lara göre hassas olabiliyor.

    keep_side:
      - "below": roof plane'in altında kalan hacmi tut (envelope mantığı)
      - "above": roof plane'in üstünü tut
    """
    agreement = True if keep_side == "below" else False
    return model.create_entity("IfcHalfSpaceSolid", BaseSurface=plane, AgreementFlag=agreement)


def _horizontal_halfspace(model, z: float, keep: str):
    """
    Horizontal plane at height z. Used to force roof to start ABOVE eaves.
    keep="above" -> keep z >= threshold (viewer uyumu için AgreementFlag ters seçilebilir)
    """
    plane = _make_ifc_plane(model, origin_xyz=(0.0, 0.0, z), normal_xyz=(0.0, 0.0, 1.0))
    agreement = False if keep == "above" else True
    return model.create_entity("IfcHalfSpaceSolid", BaseSurface=plane, AgreementFlag=agreement)


def _bool(model, op: str, a, b):
    """Create an IfcBooleanResult."""
    return model.create_entity("IfcBooleanResult", Operator=op, FirstOperand=a, SecondOperand=b)

def _assign_layer(model, items, layer_name: str):
    """
    AssignItems üzerinden layer atar.
    Bu yöntem IfcBooleanResult / IfcBooleanClippingResult gibi item'larda da çalışır.
    """
    model.create_entity(
        "IfcPresentationLayerAssignment",
        Name=layer_name,
        AssignedItems=list(items)  # <-- kritik: burada bağlanıyor
    )

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

def _hip_roof_halfspaces(model, pts2: List[Point2], h_eaves: float, slope_deg: float):
    """
    Build 4 roof planes for a hip roof:
    - 2 planes along ridge-perpendicular axis (S)
    - 2 planes along ridge axis (R)
    Planes are anchored at actual polygon support points (not bbox).
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
    pts2_local, (ox, oy) = _to_local_xy(footprint_points)
    _place_proxy_at_offset(model, proxy, ox, oy, z_dir, x_dir)

    # Attach constraints (optional metadata)
    _add_pset_roof_slopes(model, proxy, roof_slope_deg_real, roof_slope_deg_virtual)

    # Contain in storey
    _attach_proxy_to_storey(model, storey, proxy)

    # 2D profile in local coords
    profile = _make_closed_profile(model, pts2_local)
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
        halfspaces_roof, rise_max = _hip_roof_halfspaces(model, pts2_local, h_eaves, float(roof_slope_deg_real))
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
        # Layer'ı shape_rep yaratıldıktan sonra ata
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
        _place_proxy_at_offset(model, virtual_proxy, ox, oy, z_dir, x_dir)
        _attach_proxy_to_storey(model, storey, virtual_proxy)

        # Optional: also store both constraints on the virtual element
        _add_pset_roof_slopes(model, virtual_proxy, roof_slope_deg_real, roof_slope_deg_virtual)

        # Build virtual roof wedge with virtual slope
        hs_virtual, rise_v = _hip_roof_halfspaces(model, pts2_local, h_eaves, float(roof_slope_deg_virtual))
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

        # ŞEFFAFLIK + RENK (Virtual roof)
        for it in virtual_shape_rep.Items:
            _style_item(model, it, rgb=(0.2, 0.6, 1.0), transparency=0.75, name="VirtualRoofStyle")

        virtual_proxy.Representation = model.create_entity(
            "IfcProductDefinitionShape",
            Representations=[virtual_shape_rep],
        )


    # Write IFC
    model.write(out_path)
