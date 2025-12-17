# ifc_exporter.py
import ifcopenshell
import ifcopenshell.guid


def create_ifc_envelope(footprint_points, height, zone_key, out_path):
    """
    footprint_points: [(x, y), (x, y), ...]   (metre)
    height: extrude yüksekliği (metre)
    zone_key: "12a" gibi
    out_path: yazılacak IFC dosyası
    """

    model = ifcopenshell.file(schema="IFC4X3")

    # -------------------------------
    # Project + Units + Context
    # -------------------------------
    project = model.create_entity(
        "IfcProject",
        GlobalId=ifcopenshell.guid.new(),
        Name="Malgrat Envelope Project"
    )

    length_unit = model.create_entity(
        "IfcSIUnit",
        UnitType="LENGTHUNIT",
        Name="METRE",
        Prefix=None
    )

    unit_assignment = model.create_entity(
        "IfcUnitAssignment",
        Units=[length_unit]
    )
    project.UnitsInContext = unit_assignment

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

    # -------------------------------
    # Site / Building / Storey
    # -------------------------------
    site = model.create_entity(
        "IfcSite",
        GlobalId=ifcopenshell.guid.new(),
        Name="Malgrat de Mar",
        CompositionType="ELEMENT"
    )

    building = model.create_entity(
        "IfcBuilding",
        GlobalId=ifcopenshell.guid.new(),
        Name=f"Building Zone {zone_key}",
        CompositionType="ELEMENT"
    )

    storey = model.create_entity(
        "IfcBuildingStorey",
        GlobalId=ifcopenshell.guid.new(),
        Name="Ground Floor",
        Elevation=0.0,
        CompositionType="ELEMENT"
    )

    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        RelatingObject=project,
        RelatedObjects=[site]
    )
    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        RelatingObject=site,
        RelatedObjects=[building]
    )
    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        RelatingObject=building,
        RelatedObjects=[storey]
    )

    # -------------------------------
    # Envelope proxy
    # -------------------------------
    proxy = model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId=ifcopenshell.guid.new(),
        Name=f"Envelope_{zone_key}",
        ObjectType="BUILDING_ENVELOPE"
    )

    model.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=ifcopenshell.guid.new(),
        RelatingStructure=storey,
        RelatedElements=[proxy]
    )

    # -------------------------------
    # Geometry: footprint -> extrude Z by height
    # -------------------------------
    cartesian_points = []
    for x, y in footprint_points:
        cartesian_points.append(
            model.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y), 0.0))
        )

    if footprint_points[0] != footprint_points[-1]:
        first = footprint_points[0]
        cartesian_points.append(
            model.create_entity("IfcCartesianPoint", Coordinates=(float(first[0]), float(first[1]), 0.0))
        )

    polyline = model.create_entity("IfcPolyline", Points=cartesian_points)

    closed_profile = model.create_entity(
        "IfcArbitraryClosedProfileDef",
        ProfileType="AREA",
        OuterCurve=polyline
    )

    extrusion_direction = model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))

    axis_placement = model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        Axis=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0))
    )

    body_solid = model.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=closed_profile,
        Position=axis_placement,
        ExtrudedDirection=extrusion_direction,
        Depth=float(height)
    )

    shape_representation = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=[body_solid]
    )

    product_shape = model.create_entity(
        "IfcProductDefinitionShape",
        Representations=[shape_representation]
    )

    proxy.Representation = product_shape

    model.write(out_path)
