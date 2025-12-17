# regulations.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict


# -----------------------------
# Data model
# -----------------------------
@dataclass(frozen=True)
class ZoneRule:
    key: str
    max_reg_height_m: float
    max_building_depth_m: Optional[float] = None
    notes: Optional[str] = None


# -----------------------------
# Aliases (solo lo necesario)
# -----------------------------
ZONE_ALIASES: Dict[str, str] = {
    # POUM: 18c usa los mismos parámetros que 18b
    "18c": "18b",
}


# -----------------------------
# Default rule (fallback)
# -----------------------------
DEFAULT_RULE = ZoneRule(
    key="__default__",
    max_reg_height_m=10.0,      # senin istediğin gibi 10 kalsın
    max_building_depth_m=None,  # None => main.py bbox depth kullanır
    notes="Regla por defecto: zona sin normativa específica en regulations.py.",
)


# -----------------------------
# Rules (grouped by keys)
# -----------------------------
ZONE_RULES: Dict[str, ZoneRule] = {
    # ===== Key 5p =====
    "5p": ZoneRule(
        key="5p",
        max_reg_height_m=0.0,
        max_building_depth_m=0.0,
        notes="Zona de vial privado. No edificable.",
    ),

    # ===== Key 12 (Old Town) =====
    # GML: 12-1, 12-2 geliyor → aynen destekle
    "12": ZoneRule("12", max_reg_height_m=10.65, max_building_depth_m=None,
                   notes="Casco antiguo. Profundidad según planos; altura Art.75."),
    "12-1": ZoneRule("12-1", max_reg_height_m=10.65, max_building_depth_m=None,
                     notes="Subzona 12-1. Igual que 12."),
    "12-2": ZoneRule("12-2", max_reg_height_m=10.65, max_building_depth_m=None,
                     notes="Subzona 12-2. Igual que 12."),

    # ===== Key 12a (Row & Mixed Management) =====
    "12a": ZoneRule("12a", max_reg_height_m=9.00, max_building_depth_m=15.0,
                    notes="Profundidad 15m; altura 9m."),
    "112a": ZoneRule("112a", max_reg_height_m=9.15, max_building_depth_m=12.0,
                     notes="Profundidad 10–12m según planos; guardado como 12m."),
    "212a": ZoneRule("212a", max_reg_height_m=9.00, max_building_depth_m=10.0,
                     notes="Profundidad 10m; altura base 12a."),
    "312a": ZoneRule("312a", max_reg_height_m=7.00, max_building_depth_m=None,
                     notes="Altura 7m; profundidad según planos."),
    "412a": ZoneRule("412a", max_reg_height_m=9.00, max_building_depth_m=10.0,
                     notes="Altura 9m; profundidad 10m."),

    # ===== Key 13 (Densification) =====
    "13a": ZoneRule("13a", max_reg_height_m=20.75, max_building_depth_m=None,
                    notes="Densificación intensiva. Profundidad según planos."),
    "13b": ZoneRule("13b", max_reg_height_m=16.75, max_building_depth_m=None,
                    notes="Densificación semi-intensiva. Profundidad según planos."),
    "113b": ZoneRule("113b", max_reg_height_m=16.75, max_building_depth_m=None,
                     notes="Subzona 113b. Igual que 13b."),

    # ===== Key 17 (Private services) =====
    "17": ZoneRule("17", max_reg_height_m=6.10, max_building_depth_m=None,
                   notes="Servicios privados. Altura 6.10m."),

    # ===== Key 18 (Consolidated management) =====
    "18b": ZoneRule("18b", max_reg_height_m=19.80, max_building_depth_m=None,
                    notes="Altura 19.80m. (18c -> 18b por alias)"),

    # ===== Key 19 (Prefixed building) =====
    "19": ZoneRule("19", max_reg_height_m=22.85, max_building_depth_m=None,
                   notes="Altura 22.85m; resto según planos."),
    "319": ZoneRule("319", max_reg_height_m=9.90, max_building_depth_m=None,
                    notes="Igual que 19 salvo altura 9.90m."),

    # ===== Key 20 (Isolated building) =====
    "20": ZoneRule("20", max_reg_height_m=18.30, max_building_depth_m=None,
                   notes="Altura 18.30m; profundidad no fijada aquí."),
    "220a": ZoneRule("220a", max_reg_height_m=6.50, max_building_depth_m=None,
                     notes="Altura 6.50m."),

    # ===== Key 21 (Industrial) =====
    "21": ZoneRule("21", max_reg_height_m=30.0, max_building_depth_m=None,
                   notes="Base 12m; excepcionalmente hasta 30m."),
    "121": ZoneRule("121", max_reg_height_m=30.0, max_building_depth_m=None,
                    notes="Industrial PP2. Igual que 21."),
    "21a/1": ZoneRule("21a/1", max_reg_height_m=12.0, max_building_depth_m=None,
                      notes="Pequeña empresa. Altura 12m; retranqueos 5m+5m."),
    "21a/2": ZoneRule("21a/2", max_reg_height_m=12.0, max_building_depth_m=None,
                      notes="Mediana empresa. Altura 12m."),

    # ===== Key 26 (Commercial) =====
    "26": ZoneRule("26", max_reg_height_m=7.00, max_building_depth_m=None,
                   notes="Zona comercial. Altura 7m; perímetro según plano."),

    # ===== Key 30 (Complementary hotel use) =====
    "30": ZoneRule("30", max_reg_height_m=8.55, max_building_depth_m=None,
                   notes="Uso hotelero complementario. Altura 8.55m."),
}


# -----------------------------
# Helpers
# -----------------------------
def canonical_zone(zone_code: str) -> str:
    z = (zone_code or "").strip()
    return ZONE_ALIASES.get(z, z)


def get_rule(zone_code: str) -> ZoneRule:
    z = canonical_zone(zone_code)
    rule = ZONE_RULES.get(z)
    if rule is None:
        print(
            f"[AVISO] No se encontró normativa para la zona '{zone_code}' "
            f"(canónica='{z}'). Se utilizará DEFAULT_RULE."
        )
        return DEFAULT_RULE
    return rule
