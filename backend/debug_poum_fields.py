import xml.etree.ElementTree as ET
from pathlib import Path

# POUM.gml kesin backend klasöründe
POUM_PATH = Path(__file__).resolve().parent / "POUM.gml"

print("Reading POUM from:", POUM_PATH)

if not POUM_PATH.exists():
    raise FileNotFoundError(f"POUM.gml not found at: {POUM_PATH}")

tree = ET.parse(POUM_PATH)
root = tree.getroot()

ns = {"ogr": "http://ogr.maptools.org/", "gml": "http://www.opengis.net/gml/3.2"}

fm = root.find("ogr:featureMember", ns)
if fm is None or len(fm) == 0:
    raise RuntimeError("No ogr:featureMember found in POUM.gml (namespace mismatch?)")

feat = fm[0]

print("\nFIRST FEATURE CHILD TAGS (tag = value):")
for ch in list(feat):
    print(" -", ch.tag, "=", (ch.text or "").strip())

print("\nCANDIDATES containing ALT / PROF / MAX:")
for ch in list(feat):
    t = ch.tag.upper()
    if "ALT" in t or "PROF" in t or "MAX" in t:
        print(" -", ch.tag, "=", (ch.text or '').strip())
