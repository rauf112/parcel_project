# poum_index.py
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class PoumInfo:
    zone: Optional[str] = None          # C_QUAL_AJT (örn 12a, 13b, 6b, 12-2...)
    altmax_m: Optional[float] = None    # ALTMAX
    profedif_m: Optional[float] = None  # PROFEDIF (tek sayı ya da "13-13,3")
    numplamax: Optional[str] = None     # NUMPLAMAX (örn PB+4P)


def _parse_float_maybe(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None

    t = t.replace(",", ".")  # 13,3 -> 13.3

    # "13-13,3" gibi aralık gelirse max'ı al
    if "-" in t:
        parts = [p.strip() for p in t.split("-") if p.strip()]
        vals = []
        for p in parts:
            try:
                vals.append(float(p))
            except:
                pass
        return max(vals) if vals else None

    try:
        return float(t)
    except:
        return None


def build_refcat_to_poum_index(poum_gml_path: str) -> Dict[str, PoumInfo]:
    """
    POUM.gml içinden:
      RC (virgülle ayrılmış olabilir) -> PoumInfo
    sözlüğü üretir.
    """
    tree = ET.parse(poum_gml_path)
    root = tree.getroot()

    ns = {
        "ogr": "http://ogr.maptools.org/",
        "gml": "http://www.opengis.net/gml/3.2",
    }

    index: Dict[str, PoumInfo] = {}

    for fm in root.findall("ogr:featureMember", ns):
        if len(fm) == 0:
            continue
        feat = fm[0]

        zone = feat.findtext("ogr:C_QUAL_AJT", default=None, namespaces=ns)
        altmax = _parse_float_maybe(feat.findtext("ogr:ALTMAX", default=None, namespaces=ns))
        profedif = _parse_float_maybe(feat.findtext("ogr:PROFEDIF", default=None, namespaces=ns))
        numplamax = feat.findtext("ogr:NUMPLAMAX", default=None, namespaces=ns)

        rc_text = feat.findtext("ogr:RC", default=None, namespaces=ns)
        if not rc_text:
            continue

        info = PoumInfo(
            zone=zone.strip() if zone else None,
            altmax_m=altmax,
            profedif_m=profedif,
            numplamax=numplamax.strip() if numplamax else None
        )

        for rc in [x.strip() for x in rc_text.split(",")]:
            if rc:
                index[rc] = info

    return index


if __name__ == "__main__":
    idx = build_refcat_to_poum_index("POUM.gml")
    print("Index size:", len(idx))
    test = input("Test refcat: ").strip()
    print(idx.get(test))
