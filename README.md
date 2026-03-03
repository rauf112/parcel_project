# CHECKIDS

## Configuration (backend/config.json)

You can control how polygons are sourced and depth defaults via `backend/config.json`.

- `polygon_source`: one of `both` (default), `poum`, `cadastre`.
  - `both`: prefer POUM, but fallback to Cadastre WFS when POUM does not provide a usable parcel footprint.
  - `poum`: use POUM only (error if refcat not in POUM according to `poum_mode`).
  - `cadastre`: use Cadastre WFS only.

- `poum_mode`: one of `parcel` (default) or `zone`.
  - `parcel`: strict POUM matching — only use a POUM feature whose `RC` list is exactly the parcel refcat.
  - `zone`: accept POUM features that contain the refcat (may be a zoning feature); pipeline will try to intersect the POUM zone with the cadastre parcel to derive a per-parcel footprint.

- `default_depth_m`, `force_depth_m`, `ground_height`, `debug_depth_log` — other options (see file for defaults).

Behavior notes:
- When POUM returns a zoning polygon (multi-RC) and `poum_mode=zone`, the pipeline attempts a best-effort intersection of the CADASTRE parcel polygon with the POUM zone polygon to obtain a parcel-level footprint. If that fails, the CADASTRE parcel is used as a fallback. This keeps POUM as preferred source but avoids using an oversized zone polygon as a parcel footprint.

## Volume compliance check

Endpoint: `POST /check/volume-compliance`

> MVP notu: Varsayılan kontrol, mimari IFC'den zemin kat perimetresi çıkarılarak geçici hacim üretir (`method=ground_floor_perimeter_temp_volume`). Eğer bu çıkarım başarısız olursa `bbox` fallback kullanılır. Her iki yaklaşım da yaklaşık sonuç verir; kesin katı-geometri (solid boolean) analizi yapılmaz.

Request body:

```json
{
  "municipality": "Malgrat de Mar",
  "refcat": "8808517DG7180N",
  "architect_ifc_path": "backend/outputs/architect.ifc",
  "tolerance_m": 0.01,
  "keep_allowed_ifc": true
}
```

Response (MVP, standardized):

```json
{
  "compliant": true,
  "overflow_by_side_m": {
    "west": 0.0,
    "east": 0.0,
    "south": 0.0,
    "north": 0.0,
    "down": 0.0,
    "up": 0.0
  },
  "volumes": {
    "project": 1234.56,
    "intersection": 1234.56,
    "outside": 0.0
  },
  "method": "ground_floor_perimeter_temp_volume",
  "warnings": [
    "MVP bbox method: this is an approximate geometric compliance result."
  ],
  "tolerance": {
    "value_m": 0.01,
    "rule": "A side is considered non-compliant when overflow_by_side_m[side] > tolerance.value_m.",
    "overflow_exceeds_tolerance_by_side": {
      "west": false,
      "east": false,
      "south": false,
      "north": false,
      "down": false,
      "up": false
    },
    "max_overflow_m": 0.0
  },
  "sources": {
    "allowed_ifc": "...",
    "architect_ifc": "...",
    "keep_allowed_ifc": true
  }
}
```

Error semantics:
- `400`: invalid input / validation error
- `404`: file not found (ör. `architect_ifc_path`)
- `500`: internal processing error

