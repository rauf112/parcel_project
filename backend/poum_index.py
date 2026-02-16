"""
POUM index builder and polygon extractor.

Responsibilities
----------------
- Parse POUM.gml and build a {refcat: PoumInfo} lookup table.
- Extract zone codes and numeric constraints (ALTMAX, PROFEDIF).
- Provide polygon extraction for a given refcat in the POUM CRS.

Notes
-----
- All data is sourced directly from POUM.gml (no external overrides).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class PoumInfo:
    """
    POUM attributes for a parcel.

    zone: Zoning code (e.g., 12a, 13b, 12-2, 27-CP)
    altmax: Max allowed height (ALTMAX), optional
    profedif: Max allowed depth (PROFEDIF), optional
    """
    zone: Optional[str] = None          # ogr:C_QUAL_AJT
    altmax: Optional[float] = None      # ogr:ALTMAX
    profedif: Optional[float] = None    # ogr:PROFEDIF


def _to_float(x: Optional[str]) -> Optional[float]:
    """Convert a string to float (comma or dot), returning None if invalid."""
    if not x:
        return None
    x = x.strip().replace(",", ".")
    try:
        return float(x)
    except Exception:
        return None


def build_refcat_to_poum_index(poum_gml_path: str) -> Dict[str, PoumInfo]:
    """
    Build an index from POUM.gml:
        ogr:RC (may be comma-separated) -> PoumInfo(zone, altmax, profedif)

    Parsing steps:
        - root.findall("ogr:featureMember", ns)
        - feat.findtext("ogr:RC")
        - feat.findtext("ogr:C_QUAL_AJT")
        - ALTMAX / PROFEDIF are optional numeric fields
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


def get_polygon_by_refcat(poum_gml_path: str, refcat: str, strict: bool = False):
    """
    Return first polygon exterior ring coordinates for a given refcat (RC) from
    POUM.gml as a list of (x, y) tuples in the POUM file CRS (EPSG:25831).

    If strict=True, only return a polygon if the feature's RC list is exactly
    a single item matching refcat (i.e., not a grouped zoning feature).
    Returns None if not found or on parse errors.
    """
    try:
        tree = ET.parse(poum_gml_path)
        root = tree.getroot()

        ns = {"ogr": "http://ogr.maptools.org/", "gml": "http://www.opengis.net/gml/3.2"}

        for fm in root.findall("ogr:featureMember", ns):
            if len(fm) == 0:
                continue
            feat = fm[0]
            rc_text = feat.findtext("ogr:RC", default=None, namespaces=ns)
            if not rc_text:
                continue

            rcs = [x.strip() for x in rc_text.split(",")]

            if strict:
                # require exact single match
                if len(rcs) != 1 or rcs[0] != refcat:
                    continue
            else:
                if refcat not in rcs:
                    continue

            # Find first posList under this feature
            pos = feat.find(".//gml:posList", namespaces=ns)
            if pos is None or not pos.text:
                # Fallback to gml:pos sequence
                coords = []
                for pnode in feat.findall(".//gml:pos", namespaces=ns):
                    txt = pnode.text.strip() if pnode.text else ""
                    if not txt:
                        continue
                    parts = [float(x) for x in txt.split()]
                    if len(parts) >= 2:
                        coords.append((parts[0], parts[1]))
                if coords:
                    return coords
                continue

            tokens = [float(x) for x in pos.text.strip().split()]
            if len(tokens) < 6:
                continue
            pts = [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens), 2)]
            # Ensure open ring (remove duplicate last if present)
            if len(pts) >= 2 and pts[0] == pts[-1]:
                pts = pts[:-1]
            return pts
    except Exception:
        return None
    return None


if __name__ == "__main__":
    idx = build_refcat_to_poum_index("POUM.gml")
    print("Index size:", len(idx))
    test = input("Test refcat: ").strip()
    print(idx.get(test))
