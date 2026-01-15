from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class PoumInfo:
    zone: Optional[str] = None          # ogr:C_QUAL_AJT (örn 12a, 13b, 12-2, 27-CP)
    altmax: Optional[float] = None      # ogr:ALTMAX (opsiyonel)
    profedif: Optional[float] = None    # ogr:PROFEDIF (opsiyonel)


def _to_float(x: Optional[str]) -> Optional[float]:
    if not x:
        return None
    x = x.strip().replace(",", ".")
    try:
        return float(x)
    except Exception:
        return None


def build_refcat_to_poum_index(poum_gml_path: str) -> Dict[str, PoumInfo]:
    """
    POUM.gml içinden:
      ogr:RC (virgülle ayrılmış olabilir) -> PoumInfo(zone=..., altmax=..., profedif=...)
    sözlüğü üretir.

    Bu senin ORİJİNAL çalışan mantığın:
      - root.findall("ogr:featureMember", ns)
      - feat.findtext("ogr:RC")
      - feat.findtext("ogr:C_QUAL_AJT")
    Aynı kaldı, sadece ALTMAX/PROFEDIF eklendi.
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

        altmax_text = feat.findtext("ogr:ALTMAX", default=None, namespaces=ns)
        prof_text = feat.findtext("ogr:PROFEDIF", default=None, namespaces=ns)

        info = PoumInfo(
            zone=zone,
            altmax=_to_float(altmax_text),
            profedif=_to_float(prof_text),
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
