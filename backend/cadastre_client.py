# cadastre_client.py
from __future__ import annotations

import requests
import xml.etree.ElementTree as ET
from typing import List, Tuple

# Catastro INSPIRE WFS endpoint
WFS_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"


def _preview(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text[:n] + ("..." if len(text) > n else "")


def get_parcel_polygon_by_local_id(local_id: str) -> List[Tuple[float, float]]:
    """
    Obtiene el polígono de una parcela desde el WFS INSPIRE del Catastro
    usando el refcat/localId.

    Retorna: [(lon, lat), ...] en EPSG:4326

    Nota: En muchas respuestas INSPIRE, gml:posList viene como lat lon lat lon...
    Aquí lo convertimos a (lon, lat).
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
        # Algunos servicios públicos responden mejor con un UA “normal”
        "User-Agent": "Mozilla/5.0 (ParcelEnvelopeIFC/1.0)"
    }

    print(f"LocalId (refcat) enviado al WFS: {local_id}")

    resp = requests.get(WFS_URL, params=params, headers=headers, timeout=30)

    # HTTP error
    if resp.status_code != 200:
        raise RuntimeError(
            f"Error HTTP del WFS: {resp.status_code}\n"
            f"URL: {resp.url}\n"
            f"Contenido (preview):\n{_preview(resp.text)}"
        )

    # Maintenance / HTML response check
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

    # WFS ExceptionReport can be XML too
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
    test_id = input("Introduce el localId de la parcela (refcat): ").strip()
    pts = get_parcel_polygon_by_local_id(test_id)
    print("Primeros puntos:", pts[:5], "...")
