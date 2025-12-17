# cadastre_client.py
import requests
import xml.etree.ElementTree as ET
from typing import List, Tuple

WFS_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"


def get_parcel_polygon_by_local_id(local_id: str) -> List[Tuple[float, float]]:
    local_id = local_id.strip()

    params = {
        "service": "WFS",
        "request": "GetFeature",
        "version": "2.0.0",
        "STOREDQUERIE_ID": "GetParcel",
        "refcat": local_id,
        "srsname": "EPSG:4326",
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (UrbanPlanning-IFC-Academic)",
        "Accept": "application/xml",
    }

    print("LocalId (refcat) enviado al WFS:", local_id)

    resp = requests.get(WFS_URL, params=params, headers=headers, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Error HTTP {resp.status_code} del servicio WFS.\n"
            f"URL: {resp.url}\n"
            f"Respuesta (primeros 500 caracteres):\n{resp.text[:500]}"
        )

    if "text/html" in resp.headers.get("Content-Type", "").lower():
        raise RuntimeError(
            "El servicio Catastro WFS ha devuelto HTML en lugar de XML. "
            "Es posible que el servicio esté en mantenimiento."
        )

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise RuntimeError(
            "La respuesta del Catastro WFS no es un XML válido. "
            "Posible mantenimiento o error temporal del servicio.\n"
            f"Detalle: {e}\n"
            f"Contenido (primeros 500 caracteres):\n{resp.text[:500]}"
        )

    ns = {
        "gml": "http://www.opengis.net/gml/3.2",
        "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
    }

    parcels = root.findall(".//cp:CadastralParcel", ns)
    if not parcels:
        raise RuntimeError(
            "No se ha encontrado ninguna parcela (CadastralParcel) en la respuesta WFS."
        )

    pos_list_elem = parcels[0].find(".//gml:posList", ns)
    if pos_list_elem is None or not pos_list_elem.text:
        raise RuntimeError("No se ha encontrado gml:posList en la geometría de la parcela.")

    numbers = pos_list_elem.text.split()
    if len(numbers) % 2 != 0:
        raise RuntimeError("Número inválido de coordenadas en gml:posList.")

    coords: List[Tuple[float, float]] = []
    for i in range(0, len(numbers), 2):
        lat = float(numbers[i])
        lon = float(numbers[i + 1])
        coords.append((lon, lat))

    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])

    return coords
