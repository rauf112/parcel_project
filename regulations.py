# regulations.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict


@dataclass(frozen=True)
class ZoneRule:
    key: str
    max_reg_height_m: float
    max_building_depth_m: Optional[float] = None
    notes: Optional[str] = None


# Sadece gerçekten gerekli alias'lar
ZONE_ALIASES: Dict[str, str] = {
    "18c": "18b",  # POUM: 18c parametreleri 18b ile aynı
}


ZONE_RULES: Dict[str, ZoneRule] = {
    # --- Key 5p (Private road): buildable değil ---
    "5p": ZoneRule(
        key="5p",
        max_reg_height_m=0.0,
        max_building_depth_m=0.0,
        notes="Private road area. No roof/building allowed (unbuildable).",
    ),

    # --- Key 12 (Old Town) ---
    # Not: POUM bu kısımda depth'i “building parameter plans” diyor → None bırakıyoruz.
    # Height tablosunda en yüksek değer: 10.65m (PB+2P)
    "12": ZoneRule(
        key="12",
        max_reg_height_m=10.65,
        max_building_depth_m=None,
        notes="Old Town. Depth plan-dependent; height uses max from Art.75.",
    ),
    # GML kodları: 12-1, 12-2 geliyor → aynen destekle
    "12-1": ZoneRule(
        key="12-1",
        max_reg_height_m=10.65,
        max_building_depth_m=None,
        notes="Old Town subzone 12-1. Same envelope rule as Key 12.",
    ),
    "12-2": ZoneRule(
        key="12-2",
        max_reg_height_m=10.65,
        max_building_depth_m=None,
        notes="Old Town subzone 12-2. Same envelope rule as Key 12.",
    ),

    # --- Key 12a (Row & Mixed Management) ---
    "12a": ZoneRule(
        key="12a",
        max_reg_height_m=9.00,
        max_building_depth_m=15.0,
        notes="Row & Mixed Management. Depth 15.0m, height 9.00m (Art.83).",
    ),

    # Subzones of 12a
    "112a": ZoneRule(
        key="112a",
        max_reg_height_m=9.15,
        max_building_depth_m=12.0,
        notes="Depth 10 or 12m depending on plans; stored as max=12m. Height 9.15m (Art.87).",
    ),
    "212a": ZoneRule(
        key="212a",
        max_reg_height_m=9.00,
        max_building_depth_m=10.0,
        notes="Depth 10.0m (Art.88). Height assumed 9.00m (base 12a).",
    ),
    "312a": ZoneRule(
        key="312a",
        max_reg_height_m=7.00,
        max_building_depth_m=None,
        notes="Regulatory height 7.00m (Art.89). Depth plan-dependent.",
    ),
    "412a": ZoneRule(
        key="412a",
        max_reg_height_m=9.00,
        max_building_depth_m=10.0,
        notes="Height 9.00m, depth 10m (Art.90).",
    ),

    # --- Key 13 (Densification) ---
    "13a": ZoneRule(
        key="13a",
        max_reg_height_m=20.75,         # tabloda max (PB+5P)
        max_building_depth_m=None,      # depth plan-dependent
        notes="Intensive densification. Depth plan-dependent; height uses max from table.",
    ),
    "13b": ZoneRule(
        key="13b",
        max_reg_height_m=16.75,         # tabloda max (PB+4 floors)
        max_building_depth_m=None,      # depth plan-dependent
        notes="Semi-intensive densification. Depth plan-dependent; height uses max from table.",
    ),
    "113b": ZoneRule(
        key="113b",
        max_reg_height_m=16.75,
        max_building_depth_m=None,
        notes="Subzone 113b. Heights same as 13b; depth plan-dependent.",
    ),

    # --- Key 17 (Private service areas) ---
    "17": ZoneRule(
        key="17",
        max_reg_height_m=6.10,
        max_building_depth_m=None,
        notes="Private service areas. Regulatory height 6.10m (Art.105). Depth not fixed here.",
    ),

    # --- Key 18 (Consolidated management) ---
    "18b": ZoneRule(
        key="18b",
        max_reg_height_m=19.80,  # PB+5P
        max_building_depth_m=None,
        notes="Key 18b. Height 19.80m (Art.113). Depth not specified.",
    ),
    # 18c aliased to 18b via ZONE_ALIASES

    # --- Key 19 (Prefixed Building Zones) ---
    "19": ZoneRule(
        key="19",
        max_reg_height_m=22.85,  # PB+6P
        max_building_depth_m=None,
        notes="Key 19. Height uses max from table (PB+6P=22.85m). Depth plan-defined.",
    ),
    "319": ZoneRule(
        key="319",
        max_reg_height_m=9.90,
        max_building_depth_m=None,
        notes="Subzone 319: same as zone 19 except height=9.90m.",
    ),

    # --- Key 20 (Isolated building areas) ---
    "20": ZoneRule(
        key="20",
        max_reg_height_m=18.30,  # PB+5P
        max_building_depth_m=None,
        notes="Key 20. Height uses max from table (PB+5P=18.30m). Depth not fixed here.",
    ),
    "220a": ZoneRule(
        key="220a",
        max_reg_height_m=6.50,
        max_building_depth_m=None,
        notes="Zone 220a: regulatory height 6.50m (Art.143).",
    ),

    # --- Key 21 (Industrial) ---
    # POUM: prensip 12m; özel durumda 30m'ye kadar izin (Art.153.3)
    "21": ZoneRule(
        key="21",
        max_reg_height_m=30.0,
        max_building_depth_m=None,
        notes="Industrial. Base height 12m; exceptionally up to 30m (Art.153). Depth not fixed.",
    ),
    "121": ZoneRule(
        key="121",
        max_reg_height_m=30.0,
        max_building_depth_m=None,
        notes="Industrial within PP2. Same as zone 21 (Art.160).",
    ),
    "21a/1": ZoneRule(
        key="21a/1",
        max_reg_height_m=12.0,
        max_building_depth_m=None,
        notes="Small business industrial. Height 12m. Depth is plot-dependent (5m front + 5m rear setbacks).",
    ),
    "21a/2": ZoneRule(
        key="21a/2",
        max_reg_height_m=12.0,
        max_building_depth_m=None,
        notes="Medium-sized industrial. Height 12m. Depth not fixed.",
    ),

    # --- Key 26 (Commercial) ---
    "26": ZoneRule(
        key="26",
        max_reg_height_m=7.00,
        max_building_depth_m=None,
        notes="Commercial zone. Regulatory height 7.00m; depth/perimeter plan-defined.",
    ),

    # --- Key 30 (Complementary Hotel Use) ---
    "30": ZoneRule(
        key="30",
        max_reg_height_m=8.55,
        max_building_depth_m=None,
        notes="Complementary hotel use. Regulatory height 8.55m (Art.165).",
    ),
}


# Kural olmayan zone gelirse patlama yerine bunu kullanacağız
DEFAULT_RULE = ZoneRule(
    key="__default__",
    max_reg_height_m=10.0,          # istersen 0.0 yapıp “hacim üretme” yaklaşımına dönebilirsin
    max_building_depth_m=None,      # None => main.py bbox depth kullanır
    notes="Fallback rule: zone not found in regulations.py. Envelope produced using parcel bbox depth and default height.",
)


def canonical_zone(zone_code: str) -> str:
    z = (zone_code or "").strip()
    return ZONE_ALIASES.get(z, z)


def get_rule(zone_code: str) -> ZoneRule:
    z = canonical_zone(zone_code)
    rule = ZONE_RULES.get(z)
    if rule is None:
        print(
            f"[AVISO] No se encontró normativa para la zona '{zone_code}' "
            f"(canónica='{z}'). Se utilizará la regla por defecto."
        )
        return DEFAULT_RULE
    return rule