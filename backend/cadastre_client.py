# =============================================================================
# cadastre_client.py
# -----------------------------------------------------------------------------
# Connects to the Cadastre INSPIRE WFS service and fetches parcel geometry
# (polygon) using refcat (parcel code). Parses GML/XML responses and returns
# polygon coordinates in EPSG:4326 (lon, lat).
#
# Main function:
#   - get_parcel_polygon_by_local_id: Fetches polygon by refcat from WFS.
#
# Data flow:
#   - Sends HTTP GET to WFS.
#   - Parses XML response and checks for service errors.
#   - Extracts exterior ring and returns (lon, lat) coordinates.
#
# Edge cases and error handling:
#   - Raises meaningful exceptions for HTTP/XML/WFS errors and missing geometry.
#   - Validates posList order and enforces a closed ring.
# -----------------------------------------------------------------------------

from __future__ import annotations

import requests
import xml.etree.ElementTree as ET
from typing import List, Tuple

# Catastro INSPIRE WFS endpoint
WFS_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"


def _preview(text: str, n: int = 600) -> str:
    """
    Returns the first n characters of a long text (used in error previews).
    """
    text = (text or "").strip()
    return text[:n] + ("..." if len(text) > n else "")


def get_parcel_polygon_by_local_id(local_id: str) -> List[Tuple[float, float]]:
    """
    Fetches parcel polygon for the given refcat/localId from Cadastre INSPIRE WFS.
    - Input: local_id (refcat, must not be empty)
    - Output: [(lon, lat), ...] (EPSG:4326, closed ring)
    - Errors: HTTP/XML/WFS ExceptionReport, missing geometry, invalid ordering
    - Note: INSPIRE responses often return posList as lat lon lat lon...,
            it is converted to (lon, lat) here.
    """
    local_id = (local_id or "").strip()
    if not local_id:
        raise ValueError("El localId (refcat) no puede estar vacío.")

    params = {
        "service": "WFS",
        "request": "GetFeature",
        "version": "2.0.0",
        "STOREDQUERIE_ID": "GetParcel",
        "refcat": local_id,
        "srsname": "EPSG:4326",
    }

    headers = {
        # Some public services respond better with a normal User-Agent
        "User-Agent": "Mozilla/5.0 (ParcelEnvelopeIFC/1.0)"
    }

    print(f"LocalId (refcat) enviado al WFS: {local_id}")

    resp = requests.get(WFS_URL, params=params, headers=headers, timeout=30)

    # HTTP error check
    if resp.status_code != 200:
        raise RuntimeError(
            f"Error HTTP del WFS: {resp.status_code}\n"
            f"URL: {resp.url}\n"
            f"Contenido (preview):\n{_preview(resp.text)}"
        )

    # Maintenance/HTML response check
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "html" in content_type:
        raise RuntimeError(
            "El WFS devolvió HTML en vez de XML (posible mantenimiento/caída del servicio).\n"
            f"URL: {resp.url}\n"
            f"Contenido (preview):\n{_preview(resp.text)}"
        )

    # XML parse (catch invalid XML)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise RuntimeError(
            "La respuesta del WFS no es XML válido (posible mantenimiento/errores del servidor).\n"
            f"Error: {e}\n"
            f"URL: {resp.url}\n"
            f"Contenido (preview):\n{_preview(resp.text)}"
        ) from e

    ns = {
        "wfs": "http://www.opengis.net/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
        "base": "http://inspire.ec.europa.eu/schemas/base/3.3",
        "ows": "http://www.opengis.net/ows/1.1",
    }

    # WFS ExceptionReport check
    exc_text = root.findtext(".//ows:ExceptionText", default="", namespaces=ns).strip()
    if exc_text:
        raise ValueError(
            "El WFS devolvió un error (ExceptionReport).\n"
            f"Detalle: {exc_text}\n"
            f"URL: {resp.url}"
        )

    parcels = root.findall(".//cp:CadastralParcel", ns)
    if not parcels:
        raise ValueError(
            "No se encontró <cp:CadastralParcel> en la respuesta.\n"
            f"URL: {resp.url}\n"
            f"Contenido (preview):\n{_preview(resp.text)}"
        )

    pos_list_elem = parcels[0].find(".//gml:posList", ns)
    if pos_list_elem is None or not (pos_list_elem.text or "").strip():
        raise ValueError("No se encontró gml:posList para la geometría de la parcela.")

    numbers = pos_list_elem.text.split()
    if len(numbers) % 2 != 0:
        raise ValueError("posList contiene un número impar de valores (esperado lat/lon en pares).")

    coords: List[Tuple[float, float]] = []
    # INSPIRE often returns: lat lon lat lon ...
    for i in range(0, len(numbers), 2):
        lat = float(numbers[i])
        lon = float(numbers[i + 1])
        coords.append((lon, lat))

    # Ensure closed ring
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])

    print(f"Se han leído {len(coords)} vértices del polígono de la parcela.")
    return coords


if __name__ == "__main__":
    # Manual test: enter refcat and print first 5 points
    test_id = input("Introduce el localId de la parcela (refcat): ").strip()
    pts = get_parcel_polygon_by_local_id(test_id)
    print("Primeros puntos:", pts[:5], "...")
