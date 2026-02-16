from pathlib import Path
import json

BASE_DIR = Path(__file__).resolve().parent

# Load optional config.json to override defaults
_CONFIG_PATH = BASE_DIR / "config.json"
_CONFIG = {}
if _CONFIG_PATH.exists():
	try:
		_CONFIG = json.loads(_CONFIG_PATH.read_text())
	except Exception:
		_CONFIG = {}

_POUM_PATH_OVERRIDE = _CONFIG.get("poum_gml_path")
if _POUM_PATH_OVERRIDE:
	_override_path = Path(_POUM_PATH_OVERRIDE)
	POUM_GML_PATH = _override_path if _override_path.is_absolute() else (BASE_DIR / _override_path)
else:
	POUM_GML_PATH = BASE_DIR / "POUM.gml"
OUTPUT_DIR = BASE_DIR / "outputs"

DEFAULT_MUNICIPALITIES = ["Malgrat de Mar"]
