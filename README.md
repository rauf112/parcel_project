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

Response highlights:
- `compliant`: `true/false`
- `overflow_by_side_m`: taşma mesafesi (`west/east/south/north/down/up`)
- `volumes.outside_bbox_m3`: izinli hacim dışına taşan yaklaşık hacim (bbox yaklaşımı)
- `allowed_bbox` ve `project_bbox`: karşılaştırmada kullanılan kutu ölçüleri

