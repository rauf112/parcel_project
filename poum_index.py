# poum_index.py
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class PoumInfo:
    zone: Optional[str] = None  # C_QUAL_AJT (örn 12a, 13b, 6b, 12-2...)


def build_refcat_to_poum_index(poum_gml_path: str) -> Dict[str, PoumInfo]:
    """
    POUM.gml içinden:
      RC (virgülle ayrılmış olabilir) -> PoumInfo(zone=...)
    sözlüğü üretir.

    Bu projede şu an sadece zone eşlemesi gerektiği için
    ALTMAX/PROFEDIF/NUMPLAMAX vb. alanlar bilerek kaldırıldı.
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

        rc_text = feat.findtext("ogr:RC", default=None, namespaces=ns)
        if not rc_text:
            continue

        zone_text = feat.findtext("ogr:C_QUAL_AJT", default=None, namespaces=ns)
        zone = zone_text.strip() if zone_text else None

        info = PoumInfo(zone=zone)

        for rc in [x.strip() for x in rc_text.split(",")]:
            if rc:
                index[rc] = info

    return index


if __name__ == "__main__":
    idx = build_refcat_to_poum_index("POUM.gml")
    print("Index size:", len(idx))
    test = input("Test refcat: ").strip()
    print(idx.get(test))
