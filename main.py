# main.py
from __future__ import annotations

from typing import Tuple, List
from pyproj import Transformer

from cadastre_client import get_parcel_polygon_by_local_id
from poum_index import build_refcat_to_poum_index
from regulations import canonical_zone, get_rule
from ifc_exporter import create_ifc_envelope


def bbox_size_m(coords_lonlat: List[Tuple[float, float]]) -> Tuple[float, float]:
    """
    Calcula el tamaño del bounding box en metros (EPSG:25831).
    Retorna: (ancho_m, profundidad_m)
    """
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25831", always_xy=True)

    xs, ys = [], []
    for lon, lat in coords_lonlat:
        x, y = transformer.transform(lon, lat)
        xs.append(x)
        ys.append(y)

    return max(xs) - min(xs), max(ys) - min(ys)


def bbox_footprint(coords_lonlat: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Genera un footprint rectangular simple (EPSG:25831) para el envelope IFC.
    """
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25831", always_xy=True)

    xs, ys = [], []
    for lon, lat in coords_lonlat:
        x, y = transformer.transform(lon, lat)
        xs.append(x)
        ys.append(y)

    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)

    return [
        (minx, miny),
        (maxx, miny),
        (maxx, maxy),
        (minx, maxy),
    ]


def main() -> None:
    refcat = input("Introduce el localId de la parcela (refcat): ").strip()
    if not refcat:
        print("[ERROR] El refcat no puede estar vacío.")
        return

    # 1) Catastro: obtener polígono de la parcela
    try:
        coords_lonlat = get_parcel_polygon_by_local_id(refcat)
    except Exception as e:
        print("[ERROR] No se pudo obtener la parcela desde el WFS del Catastro.")
        print("Motivo:", str(e))
        print("Sugerencia: el servicio puede estar en mantenimiento. Inténtalo más tarde.")
        return

    print(f"Se han leído {len(coords_lonlat)} vértices de la parcela.")

    # 2) POUM: refcat -> zona
    idx = build_refcat_to_poum_index("POUM.gml")
    poum_info = idx.get(refcat)

    zone_raw = (poum_info.zone if poum_info else None) or "UNKNOWN"
    zone_key = canonical_zone(zone_raw)
    print(f"Zona urbanística: {zone_key}")

    # 3) Bounding box y footprint
    width_m, depth_m = bbox_size_m(coords_lonlat)
    footprint = bbox_footprint(coords_lonlat)

    print(
        f"Dimensiones del bounding box de la parcela (en metros): "
        f"{width_m:.2f} m x {depth_m:.2f} m"
    )

    # 4) Normativa urbanística (con fallback)
    rule = get_rule(zone_key)

    building_depth_m = (
        rule.max_building_depth_m
        if rule.max_building_depth_m is not None
        else depth_m
    )
    building_height_m = rule.max_reg_height_m

    print(f"Profundidad edificable (según normativa): {building_depth_m:.2f} m")
    print(f"Altura reguladora (según normativa): {building_height_m:.2f} m")

    # 5) Zona no edificable
    if building_height_m <= 0.0 or building_depth_m <= 0.0:
        print("[INFO] Parcela no edificable. No se genera archivo IFC.")
        return

    # 6) Generar IFC
    out_path = f"malgrat_{refcat}_{zone_key}_envelope.ifc"
    create_ifc_envelope(
        footprint_points=footprint,
        height=building_height_m,
        zone_key=zone_key,
        out_path=out_path,
    )

    print(f"Archivo IFC generado: {out_path}")


if __name__ == "__main__":
    main()
