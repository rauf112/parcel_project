import xml.etree.ElementTree as ET
from pathlib import Path

POUM_PATH = Path(__file__).resolve().parent / "POUM.gml"
print("Reading POUM from:", POUM_PATH)

tree = ET.parse(POUM_PATH)
root = tree.getroot()

ns = {"ogr": "http://ogr.maptools.org/", "gml": "http://www.opengis.net/gml/3.2"}

hits = 0
max_hits = 20  # ilk 20 örnek yeter

for fm in root.findall("ogr:featureMember", ns):
    if len(fm) == 0:
        continue
    feat = fm[0]

    rc_text = (feat.findtext("ogr:RC", default="", namespaces=ns) or "").strip()
    if not rc_text:
        continue

    zone = (feat.findtext("ogr:C_QUAL_AJT", default="", namespaces=ns) or "").strip()
    alt = (feat.findtext("ogr:ALTMAX", default="", namespaces=ns) or "").strip()
    prof = (feat.findtext("ogr:PROFEDIF", default="", namespaces=ns) or "").strip()

    # değer doluysa yazdır
    if alt or prof:
        print("\nFOUND:")
        print(" zone     =", zone)
        print(" ALTMAX   =", alt)
        print(" PROFEDIF =", prof)
        print(" RC       =", rc_text)
        hits += 1
        if hits >= max_hits:
            break

print("\nTOTAL_FOUND_SHOWN =", hits)
