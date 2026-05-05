# CHECKIDS

## Configuration (`backend/config.json`)

All pipeline behaviour is controlled by `backend/config.json`. Below is a description of every parameter.

---

### Polygon source

| Parameter | Type | Default | Description |
|---|---|---|---|
| `polygon_source` | `"poum"` \| `"cadastre"` \| `"both"` | `"both"` | Which data source to use for the parcel footprint polygon. `"both"` prefers POUM and falls back to Cadastre WFS when POUM does not return a usable polygon. `"poum"` raises an error if the parcel is not found in POUM. `"cadastre"` always uses the Cadastre WFS. |
| `poum_mode` | `"parcel"` \| `"zone"` | `"parcel"` | How to match parcels inside the POUM file. `"parcel"` requires the POUM feature's `RC` list to match the refcat exactly. `"zone"` accepts any POUM zone that contains the refcat (looser match, useful when the POUM only defines zoning polygons rather than individual parcels). |
| `poum_zone_area_ratio_threshold` | number | `100.0` | When `poum_mode="zone"`, if the POUM zone area divided by the cadastre parcel area exceeds this ratio, the zone is considered too large and the pipeline falls back to the cadastre polygon. Prevents using enormous zoning polygons as a parcel footprint. |
| `poum_simplify_zone` | boolean | `true` | If `true`, simplify the POUM zone polygon before intersecting it with the cadastre parcel. Reduces vertex noise from large zone outlines. |
| `poum_simplify_method` | string | `"convex_hull"` | Simplification method applied when `poum_simplify_zone=true`. Currently supports `"convex_hull"`. |
| `poum_zone_intersection` | boolean | `false` | If `true`, intersect the POUM zone polygon with the cadastre parcel polygon to derive a parcel-level footprint from a zone-level POUM feature. Useful when `poum_mode="zone"`. |

---

### Building depth

| Parameter | Type | Default | Description |
|---|---|---|---|
| `default_depth_m` | number \| null | `null` | Default building depth in metres used when depth cannot be derived from street analysis or POUM regulations. Set to `null` to have no fallback (pipeline will raise an error if depth is missing). |
| `force_depth_m` | boolean | `false` | If `true`, always override the computed depth with `default_depth_m` regardless of what street analysis returns. Useful for testing or for municipalities where depth rules are not modelled in POUM. |

---

### Height and roof

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ground_height` | number | `1.0` | Vertical offset (metres) applied to the base of the generated IFC envelope so it sits above the cadastre ground slab. |
| `roof_rise_max_m` | number | `20.0` | Maximum allowed roof ridge rise in metres above the eaves. Acts as a safety cap to prevent geometrically degenerate roof shapes when slope rules produce very tall ridges. |

---

### Debugging

| Parameter | Type | Default | Description |
|---|---|---|---|
| `debug_depth_log` | boolean | `true` | If `true`, prints detailed logs about how building depth and polygon source were selected for each parcel. Useful for diagnosing unexpected geometry results. |

---

### Preprocess (simplified cadastre generation)

These parameters control the `/preprocess/simplifycadastre` step, which generates a pre-processed JSON file with simplified parcel geometries and street metadata. The generate step can optionally read from this file instead of querying the Cadastre WFS live.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `preprocess_source` | `"poum"` \| `"cadastre"` \| `"both"` | `"both"` | Polygon source to use when building the simplified cadastre file. Same semantics as `polygon_source`. |
| `preprocess_poum_mode` | `"parcel"` \| `"zone"` | `"parcel"` | POUM matching mode used during preprocessing. Same semantics as `poum_mode`. |
| `preprocess_output_path` | string | `"outputs/parcels_simplified.json"` | File path (relative to the backend folder) where the simplified cadastre JSON is written. |
| `preprocess_street_max_distance_m` | number | `30.0` | Maximum ray distance in metres when searching for the nearest street segment from a parcel edge midpoint. Segments farther than this are ignored. |
| `preprocess_street_offset_m` | number | `0.1` | Small offset (metres) applied to the ray origin before casting, to avoid self-intersecting with the parcel boundary itself. |
| `preprocess_vertex_angle_threshold_rad` | number | `0.1` | Collinearity threshold in radians. Parcel boundary vertices whose interior angle is smaller than this value are removed during simplification. |
| `generate_use_preprocess_geometry` | boolean | `true` | If `true`, the envelope generation step reads parcel geometry and street metadata from the preprocess JSON file (if it exists) instead of querying the Cadastre WFS live. Significantly speeds up batch generation. |

---

### Deprecated keys

The following keys are accepted for backwards compatibility but should not be used in new configurations. They map to the `preprocess_*` equivalents above.

`simplify_source` → `preprocess_source`  
`simplify_poum_mode` → `preprocess_poum_mode`  
`simplify_output_path` → `preprocess_output_path`  
`street_max_distance_m` → `preprocess_street_max_distance_m`  
`street_offset_m` → `preprocess_street_offset_m`  
`vertex_angle_threshold_rad` → `preprocess_vertex_angle_threshold_rad`

---

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

