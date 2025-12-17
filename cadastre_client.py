# cadastre_client.py
from __future__ import annotations

import requests
import xml.etree.ElementTree as ET
from typing import List, Tuple

# ✅ Catastro INSPIRE WFS endpoint
WFS_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"


def _preview(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text[:n] + ("..." if len(text) > n else "")


def get_parcel_polygon_by_local_id(local_id: str) -> List[Tuple[float, float]]:
    """
    Catastro INSPIRE WFS üzerinden, refcat/local_id ile parsel polygon'u çeker.
    Dönüş: [(lon, lat), ...]  EPSG:4326

    StoredQuery: GetParcel + refcat paramı.
    Not: Bazı cevaplarda posList sırası lat lon lat lon ... gelebiliyor (biz öyle parse ediyoruz).
    """
    local_id = (local_id or "").strip()
    if not local_id:
        raise ValueError("local_id (refcat) boş olamaz.")

    params = {
        "service": "WFS",
        "request": "GetFeature",
        "version": "2.0.0",
        "STOREDQUERIE_ID": "GetParcel",
        "refcat": local_id,
        "srsname": "EPSG:4326",
    }

    headers = {
        # Bazı kamu servisleri default Python UA'yı sevmiyor; HTML/redirect dönebiliyor.
        "User-Agent": "Mozilla/5.0 (ParcelEnvelopeIFC/1.0)"
    }

    print("LocalId (refcat) enviado al WFS:", local_id)

    resp = requests.get(WFS_URL, params=params, headers=headers, timeout=30)

    # HTTP hata
    if resp.status_code != 200:
        raise RuntimeError(
            f"WFS HTTP {resp.status_code}\n"
            f"URL: {resp.url}\n"
            f"Body (preview):\n{_preview(resp.text)}"
        )

    # Content-Type kontrolü (maintenance sırasında HTML dönebiliyor)
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "html" in content_type:
        raise RuntimeError(
            "El WFS devolvió HTML en vez de XML (posible mantenimiento/caída del servicio).\n"
            f"URL: {resp.url}\n"
            f"Body (preview):\n{_preview(resp.text)}"
        )

    # XML parse (bozuk XML yakala)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise RuntimeError(
            "Respuesta del WFS no es XML válido (posible mantenimiento/errores del servidor).\n"
            f"Error: {e}\n"
            f"URL: {resp.url}\n"
            f"Body (preview):\n{_preview(resp.text)}"
        ) from e

    ns = {
        "wfs": "http://www.opengis.net/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
        "base": "http://inspire.ec.europa.eu/schemas/base/3.3",
        "ows": "http://www.opengis.net/ows/1.1",
    }

    # WFS ExceptionReport kontrolü (XML ama hata olabilir)
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
            f"Body (preview):\n{_preview(resp.text)}"
        )

    pos_list_elem = parcels[0].find(".//gml:posList", ns)
    if pos_list_elem is None or not (pos_list_elem.text or "").strip():
        raise ValueError("No se encontró gml:posList para la geometría de la parcela.")

    numbers = pos_list_elem.text.split()
    if len(numbers) % 2 != 0:
        raise ValueError("posList contiene un número impar de valores (esperado lat/lon en pares).")

    coords: List[Tuple[float, float]] = []
    # Senin örneklerde posList: lat lon lat lon ... geliyordu
    for i in range(0, len(numbers), 2):
        lat = float(numbers[i])
        lon = float(numbers[i + 1])
        coords.append((lon, lat))

    # Kapalı ring garantisi
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])

    return coords


if __name__ == "__main__":
    test_id = input("Introduce el localId de la parcela (refcat): ").strip()
    pts = get_parcel_polygon_by_local_id(test_id)
    print(f"Se han leído {len(pts)} vértices de la parcela.")
    print("Primeros puntos:", pts[:5], "...")
