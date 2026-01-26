import re
import json
from pathlib import Path
import fitz  # PyMuPDF

PDF_PATH = r"C:/Users/rauf1/OneDrive/Masaüstü/WORK!/backend/2024.08.23_POUM_Urban_Regulations.en.pdf"
OUT_PATH = "poum_zones_height_depth.real_sections_v2.json"

# 1) "section" başlıklarını daha geniş yakala:
# - "Fifth section: ... (key 12)"
# - "Section Ten: ... (Key 18)"
# - "Section 1: ... (key 21)"
# - "Section eleven: ... (keys 19)"
SECTION_HEADER_FLEX_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth)\s+section
        |
        Section\s+(?:\d+|One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve)
        |
        Section\s+(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)
    )
    \s*:\s*
    .*?
    \(\s*key(?:s)?\s+(?P<key>\d+[a-z]?(?:/\d+)?)\s*\)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE
)

# Index satırlarını ele
INDEX_SMELL_RE = re.compile(r"(Art\.|Arts\.|\.{3,}|\s\d{1,3}\s*$)", re.IGNORECASE)

# Height / Depth
HEIGHT_RE_LIST = [
    re.compile(r"maximum\s+regulatory\s+height[^0-9]*([\d]+(?:[.,]\d+)?)\s*m", re.IGNORECASE),
    re.compile(r"\bregulatory\s+height\s+(?:will\s+be|is|of)\s*[^0-9]*([\d]+(?:[.,]\d+)?)\s*m", re.IGNORECASE),
    re.compile(r"\bRegulatory\s+height:\s*([\d]+(?:[.,]\d+)?)\s*m", re.IGNORECASE),
]
DEPTH_RE_LIST = [
    re.compile(r"maximum\s+building\s+depth[^0-9]*([\d]+(?:[.,]\d+)?)\s*m", re.IGNORECASE),
    re.compile(r"\bbuilding\s+depth\s+(?:will\s+be|is)\s*[^0-9]*([\d]+(?:[.,]\d+)?)\s*m", re.IGNORECASE),
    re.compile(r"\bbuildable\s+depth\s+(?:will\s+be|is)\s*[^0-9]*([\d]+(?:[.,]\d+)?)\s*m", re.IGNORECASE),
]
SUBZONE_RE = re.compile(r"\bsubzone\s+(\d+[a-z])\b", re.IGNORECASE)

def to_float(x: str) -> float:
    return float(x.replace(",", "."))

def pick_first_number(regex_list, text: str):
    for rx in regex_list:
        m = rx.search(text)
        if m:
            return to_float(m.group(1))
    return None

def load_pages_text(pdf_path: str):
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        pages.append((i + 1, doc.load_page(i).get_text("text")))
    return doc, pages

def find_key_starts(pages):
    key_to_start = {}
    key_to_line = {}

    for pno, txt in pages:
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        for ln in lines:
            # index kokusu varsa alma
            if INDEX_SMELL_RE.search(ln):
                continue

            m = SECTION_HEADER_FLEX_RE.match(ln)
            if not m:
                continue

            key = m.group("key")
            # aynı key'i bir kere al
            if key not in key_to_start:
                key_to_start[key] = pno
                key_to_line[key] = ln

    return key_to_start, key_to_line

def build_ranges(key_to_start, total_pages):
    # start sayfaya göre sırala
    items = sorted(key_to_start.items(), key=lambda x: (x[1], x[0]))

    ranges = {}
    for i, (k, start) in enumerate(items):
        # bir sonraki key’in start’ına kadar
        if i + 1 < len(items):
            next_start = items[i + 1][1]
            end = max(start, next_start - 1)
        else:
            end = total_pages
        ranges[k] = (start, end)

    return ranges

def extract_zone_data(pages_dict, ranges):
    out = {}
    for key, (start, end) in ranges.items():
        chunk = [(p, pages_dict[p]) for p in range(start, end + 1) if p in pages_dict]
        combined = "\n".join(t for _, t in chunk)

        default_h = pick_first_number(HEIGHT_RE_LIST, combined)
        default_d = pick_first_number(DEPTH_RE_LIST, combined)

        # overhang 1.00/1.20 m gibi şeyler depth sanılmasın
        if default_d is not None and default_d < 4.0:
            default_d = None

        sub_map = {}
        for pno, text in chunk:
            parts = re.split(r"\n{2,}|(?=Art\.\s*\d+)|(?=Article\s+\d+)", text)
            for part in parts:
                sm = SUBZONE_RE.search(part)
                if not sm:
                    continue
                subz = sm.group(1)

                h = pick_first_number(HEIGHT_RE_LIST, part)
                d = pick_first_number(DEPTH_RE_LIST, part)
                if d is not None and d < 4.0:
                    d = None

                sub_map.setdefault(subz, {"max_height_m": None, "max_depth_m": None, "sources": []})
                if h is not None:
                    sub_map[subz]["max_height_m"] = h
                if d is not None:
                    sub_map[subz]["max_depth_m"] = d
                if (h is not None) or (d is not None):
                    sub_map[subz]["sources"].append(f"p{pno}")

        out[key] = {
            "_meta": {"range": [start, end], "sources": [f"p{p}" for p in range(start, end + 1)]},
            "_default": {
                "max_height_m": default_h,
                "max_depth_m": default_d,
                "sources": [f"p{p}" for p in range(start, end + 1)] if (default_h is not None or default_d is not None) else [],
            },
        }
        for subz, data in sub_map.items():
            out[key][subz] = data

    return out

def main():
    doc, pages = load_pages_text(PDF_PATH)
    pages_dict = {p: t for p, t in pages}

    key_to_start, key_to_line = find_key_starts(pages)

    if not key_to_start:
        print("ERROR: Gerçek section header bulunamadı.")
        return

    ranges = build_ranges(key_to_start, doc.page_count)

    print("=== REAL SECTION HEADERS FOUND ===")
    for k, s in sorted(key_to_start.items(), key=lambda x: x[1]):
        print(f"Key {k} starts at p{s}: {key_to_line[k]}")

    data = extract_zone_data(pages_dict, ranges)
    Path(OUT_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote: {Path(OUT_PATH).resolve()}")

if __name__ == "__main__":
    main()
