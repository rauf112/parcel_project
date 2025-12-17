# cadastre_client.py
import requests
import xml.etree.ElementTree as ET
from typing import List, Tuple

# Endpoint INSPIRE Catastro
WFS_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"


def get_parcel_polygon_by_local_id(local_id: str) -> List[Tuple[float, float]]:
    """
    Obtiene el polígono de una parcela desde el WFS INSPIRE del Catastro
    usando el refcat/localId.

    Retorna: [(lon, lat), ...] en EPSG:4326
    """
    local_id = local_id.strip()

    params = {
        "service": "WFS",
        "request": "GetFeature",
        "version": "2.0.0",
        "STOREDQUERIE_ID": "GetParcel",
        "refcat": local_id,
        "srsname": "EPSG:4326",
    }

    print(f"LocalId (refcat) enviado al WFS: {local_id}")

    resp = requests.get(WFS_URL, params=params, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Error HTTP del WFS: {resp.status_code}\n"
            f"URL: {resp.url}\n"
            f"Contenido (primeros 500 caracteres):\n{resp.text[:500]}"
        )

    root = ET.fromstring(resp.content)

    ns = {
        "wfs": "http://www.opengis.net/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
        "base": "http://inspire.ec.europa.eu/schemas/base/3.3",
    }

    parcels = root.findall(".//cp:CadastralParcel", ns)
    if not parcels:
        raise ValueError(
            "No se ha encontrado ningún elemento CadastralParcel. "
            "Revisa el formato de la respuesta del WFS."
        )

    pos_list_elem = parcels[0].find(".//gml:posList", ns)
    if pos_list_elem is None or not pos_list_elem.text:
        raise ValueError("No se ha encontrado el elemento gml:posList.")

    numbers = pos_list_elem.text.split()
    if len(numbers) % 2 != 0:
        raise ValueError(
            "El número de coordenadas en posList no es par."
        )

    coords: List[Tuple[float, float]] = []

    # INSPIRE suele devolver: lat lon lat lon ...
    for i in range(0, len(numbers), 2):
        lat = float(numbers[i])
        lon = float(numbers[i + 1])
        coords.append((lon, lat))

    # Cerrar el polígono si no viene cerrado
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])

    print(f"Se han leído {len(coords)} vértices del polígono de la parcela.")
    return coords


if __name__ == "__main__":
    test_id = input("Introduce un localId (refcat) para prueba: ").strip()
    pts = get_parcel_polygon_by_local_id(test_id)
    print("Primeras coordenadas:", pts[:5], "...")
