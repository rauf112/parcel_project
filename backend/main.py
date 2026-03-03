"""
FastAPI entrypoint for the parcel envelope generation service.

Responsibilities
----------------
- Serves a small UI from /static and the root path.
- Exposes endpoints to list municipalities/parcels and to generate IFC envelopes.
- Runs long-running generation tasks in background threads (per job).
- Provides job status, logs, and download links for generated IFC files.

Notes
-----
- Job state is kept in memory (see jobs.py); restarting the process clears jobs.
- Batch generation uses throttling constants to avoid WFS overload.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from pathlib import Path
import threading
import time
import json

from config import POUM_GML_PATH, OUTPUT_DIR, DEFAULT_MUNICIPALITIES
from jobs import create_job, get_job, append_log, Job
from pipeline import list_refcats_from_poum, generate_one
from simplify_cadastre_like import generate_simplified_cadastre_like_file
from volume_compliance import run_volume_compliance_check

app = FastAPI(title="Parcel BIM/GIS Automation API", version="1.0")

# --- Serve UI from / (backend/static/index.html) ---
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def ui_home():
    """Serve the UI index.html file."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health", include_in_schema=False)
def health():
    """Simple health check endpoint."""
    return {"ok": True, "message": "Backend is running."}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MUNICIPALITY_TO_SLUG = {"Malgrat de Mar": "malgrat"}


class GenerateRequest(BaseModel):
    """Request body for /generate."""
    municipality: str
    all_parcels: bool
    refcat: Optional[str] = None


class GenerateResponse(BaseModel):
    """Response body for /generate."""
    job_id: str


class SimplifyCadastreRequest(BaseModel):
    municipality: str
    preprocess_source: Optional[str] = None
    preprocess_poum_mode: Optional[str] = None
    preprocess_output_path: Optional[str] = None
    preprocess_street_max_distance_m: Optional[float] = None
    preprocess_street_offset_m: Optional[float] = None
    preprocess_vertex_angle_threshold_rad: Optional[float] = None

    source: Optional[str] = None
    poum_mode: Optional[str] = None
    output_path: Optional[str] = None
    max_distance_m: Optional[float] = None
    offset_distance_m: Optional[float] = None
    angle_threshold_rad: Optional[float] = None


class VolumeComplianceRequest(BaseModel):
    municipality: str
    refcat: str
    architect_ifc_path: str
    tolerance_m: Optional[float] = 0.01
    keep_allowed_ifc: Optional[bool] = True


@app.get("/municipalities")
def get_municipalities() -> List[str]:
    """Return supported municipalities configured for this instance."""
    return DEFAULT_MUNICIPALITIES


@app.get("/parcels")
def get_parcels(municipality: str) -> List[str]:
    """Return parcel refcats for a municipality, backed by POUM data."""
    if municipality not in DEFAULT_MUNICIPALITIES:
        raise HTTPException(status_code=400, detail="Unknown municipality")
    try:
        return list_refcats_from_poum(POUM_GML_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read POUM: {e}")


def _load_backend_config() -> Dict[str, Any]:
    config_path = Path(__file__).resolve().parent / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run_simplify_cadastre_job(
    job: Job,
    *,
    source: str,
    poum_mode: str,
    output_path: Path,
    max_distance: float,
    offset_distance: float,
    angle_threshold: float,
) -> None:
    """Execute simplify-cadastre preprocessing in background and update job state."""
    job.status = "running"
    job.started_at = time.time()
    append_log(job, "SimplifyCadastre job started")

    try:
        append_log(job, f"source={source} | poum_mode={poum_mode}")
        append_log(job, f"output={output_path}")

        result = generate_simplified_cadastre_like_file(
            poum_gml_path=str(POUM_GML_PATH),
            output_file_path=str(output_path),
            source=source,
            poum_mode=poum_mode,
            max_distance=max_distance,
            offset_distance=offset_distance,
            angle_threshold=angle_threshold,
        )

        output_file = result.get("output_file")
        parcel_count = result.get("parcel_count")
        street_engine_used = result.get("street_engine_used")

        if output_file:
            job.files = [str(output_file)]

        job.progress = 1.0
        job.status = "success"
        job.finished_at = time.time()
        job.message = f"Done. Generated simplified file with {parcel_count} parcels."
        append_log(job, job.message)
        if street_engine_used:
            append_log(job, f"street_engine_used={street_engine_used}")

    except Exception as e:
        job.status = "error"
        job.finished_at = time.time()
        job.message = str(e)
        append_log(job, f"ERROR: {str(e)}")


@app.post("/preprocess/simplifycadastre", response_model=GenerateResponse)
def post_simplify_cadastre(req: SimplifyCadastreRequest):
    if req.municipality not in DEFAULT_MUNICIPALITIES:
        raise HTTPException(status_code=400, detail="Unknown municipality")

    cfg = _load_backend_config()

    def _cfg_first(*keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in cfg and cfg.get(key) is not None:
                return cfg.get(key)
        return default

    source = (
        (req.preprocess_source if req.preprocess_source is not None else None)
        or (req.source if req.source is not None else None)
        or _cfg_first("preprocess_source", "simplify_source", default="both")
    ).strip().lower()
    if source not in {"poum", "cadastre", "both"}:
        raise HTTPException(status_code=400, detail="source must be one of: poum, cadastre, both")

    poum_mode = (
        (req.preprocess_poum_mode if req.preprocess_poum_mode is not None else None)
        or (req.poum_mode if req.poum_mode is not None else None)
        or _cfg_first("preprocess_poum_mode", "simplify_poum_mode", default="parcel")
    ).strip().lower()
    if poum_mode not in {"parcel", "zone"}:
        raise HTTPException(status_code=400, detail="poum_mode must be one of: parcel, zone")

    output_path_cfg = (
        (req.preprocess_output_path if req.preprocess_output_path is not None else None)
        or (req.output_path if req.output_path is not None else None)
        or _cfg_first("preprocess_output_path", "simplify_output_path", default="outputs/parcels_simplified.json")
    )
    output_path = Path(output_path_cfg)
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parent / output_path

    max_distance = float(
        req.preprocess_street_max_distance_m
        if req.preprocess_street_max_distance_m is not None
        else req.max_distance_m
        if req.max_distance_m is not None
        else _cfg_first("preprocess_street_max_distance_m", "street_max_distance_m", default=30.0)
    )
    offset_distance = float(
        req.preprocess_street_offset_m
        if req.preprocess_street_offset_m is not None
        else req.offset_distance_m
        if req.offset_distance_m is not None
        else _cfg_first("preprocess_street_offset_m", "street_offset_m", default=0.1)
    )
    angle_threshold = float(
        req.preprocess_vertex_angle_threshold_rad
        if req.preprocess_vertex_angle_threshold_rad is not None
        else req.angle_threshold_rad
        if req.angle_threshold_rad is not None
        else _cfg_first("preprocess_vertex_angle_threshold_rad", "vertex_angle_threshold_rad", default=0.1)
    )
    job = create_job(req.municipality, all_parcels=False, refcat=None)

    t = threading.Thread(
        target=run_simplify_cadastre_job,
        kwargs={
            "job": job,
            "source": source,
            "poum_mode": poum_mode,
            "output_path": output_path,
            "max_distance": max_distance,
            "offset_distance": offset_distance,
            "angle_threshold": angle_threshold,
        },
        daemon=True,
    )
    t.start()

    return GenerateResponse(job_id=job.id)


@app.post("/check/volume-compliance")
def post_volume_compliance(req: VolumeComplianceRequest) -> Dict[str, Any]:
    if req.municipality not in DEFAULT_MUNICIPALITIES:
        raise HTTPException(status_code=400, detail="Unknown municipality")

    refcat = (req.refcat or "").strip()
    if not refcat:
        raise HTTPException(status_code=400, detail="refcat is required")

    architect_ifc_path = (req.architect_ifc_path or "").strip()
    if not architect_ifc_path:
        raise HTTPException(status_code=400, detail="architect_ifc_path is required")

    tolerance = float(req.tolerance_m if req.tolerance_m is not None else 0.01)
    if tolerance < 0:
        raise HTTPException(status_code=400, detail="tolerance_m must be >= 0")

    try:
        return run_volume_compliance_check(
            municipality=req.municipality,
            refcat=refcat,
            architect_ifc_path=architect_ifc_path,
            tolerance_m=tolerance,
            keep_allowed_ifc=bool(req.keep_allowed_ifc),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Volume compliance check failed: {e}")


# -------- Batch tuning knobs --------
# The following values throttle batch requests to avoid overloading WFS.
CHUNK_SIZE = 20          # Number of parcels per batch
REQUEST_DELAY = 0.30     # Delay (s) after each WFS call
CHUNK_DELAY = 1.50       # Delay (s) after each batch
MAX_FAILS = 200          # Abort job if failures exceed this


def _chunks(lst: List[str], n: int):
    """Yield list chunks of size n."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def run_job(job: Job) -> None:
    """Execute a job (single or batch) and update its state/logs in place."""
    job.status = "running"
    job.started_at = time.time()
    append_log(job, "Job started")

    try:
        municipality_slug = MUNICIPALITY_TO_SLUG.get(job.municipality, "municipality")

        # -------------------- ALL PARCELS (BATCH) --------------------
        if job.all_parcels:
            refcats = list_refcats_from_poum(POUM_GML_PATH)
            total = max(len(refcats), 1)
            append_log(job, f"Batch mode: {len(refcats)} parcels")
            job.meta = {
                "mode": "batch",
                "preprocess_geometry_used_count": 0,
                "preprocess_geometry_source_files": [],
            }

            produced_paths: List[str] = []
            fails = 0
            done = 0

            for batch_i, group in enumerate(_chunks(refcats, CHUNK_SIZE), start=1):
                append_log(job, f"--- Batch {batch_i} ({len(group)} parcels) ---")

                for refcat in group:
                    try:
                        append_log(job, f"[{done+1}/{total}] Generating IFC for {refcat}...")

                        result = generate_one(
                            refcat=refcat,
                            poum_gml_path=POUM_GML_PATH,
                            output_dir=OUTPUT_DIR,
                            municipality_slug=municipality_slug,
                        )

                        # diagnostic log
                        append_log(job, f"Zone={result.get('zone')} | rule_sources={result.get('rule_sources')}")

                        if result.get("used_preprocess_geometry"):
                            job.meta["preprocess_geometry_used_count"] = int(job.meta.get("preprocess_geometry_used_count", 0)) + 1
                            src = result.get("preprocess_source_file")
                            if src:
                                existing = set(job.meta.get("preprocess_geometry_source_files", []))
                                existing.add(str(src))
                                job.meta["preprocess_geometry_source_files"] = sorted(existing)

                        if not result.get("skipped") and result.get("ifc_path"):
                            produced_paths.append(result["ifc_path"])

                    except Exception as e:
                        fails += 1
                        append_log(job, f"ERROR rc={refcat} -> {type(e).__name__}: {e}")

                        # Abort if too many failures
                        if fails >= MAX_FAILS:
                            job.status = "error"
                            job.finished_at = time.time()
                            job.message = f"Too many failures ({fails}). Stopping."
                            append_log(job, job.message)
                            job.progress = done / total if total else 1.0
                            return

                    finally:
                        done += 1
                        job.progress = done / total if total else 1.0
                        time.sleep(REQUEST_DELAY)

                time.sleep(CHUNK_DELAY)

            job.files = produced_paths
            job.status = "success"
            job.message = f"Done. Produced {len(produced_paths)} IFC files. Failed: {fails}."
            append_log(job, job.message)

        # -------------------- SINGLE PARCEL --------------------
        else:
            if not job.refcat:
                raise HTTPException(status_code=400, detail="refcat is required when all_parcels=false")

            append_log(job, f"Single mode: {job.refcat}")

            result = generate_one(
                refcat=job.refcat,
                poum_gml_path=POUM_GML_PATH,
                output_dir=OUTPUT_DIR,
                municipality_slug=municipality_slug,
            )

            # diagnostic log
            append_log(job, f"Zone={result.get('zone')} | rule_sources={result.get('rule_sources')}")
            job.meta = {
                "mode": "single",
                "used_preprocess_geometry": bool(result.get("used_preprocess_geometry")),
                "preprocess_source_file": result.get("preprocess_source_file"),
            }

            if result.get("skipped"):
                job.files = []
                job.status = "success"
                job.message = f"Skipped (non-buildable). Zone={result.get('zone')}"
                append_log(job, job.message)
            else:
                job.files = [result["ifc_path"]]
                job.status = "success"
                job.message = "Done. Produced 1 IFC file."
                append_log(job, job.message)

            job.progress = 1.0

        job.finished_at = time.time()

    except HTTPException as he:
        job.status = "error"
        job.finished_at = time.time()
        job.message = str(he.detail)
        append_log(job, f"ERROR: {he.detail}")

    except Exception as e:
        job.status = "error"
        job.finished_at = time.time()
        job.message = str(e)
        append_log(job, f"ERROR: {str(e)}")


@app.post("/generate", response_model=GenerateResponse)
def post_generate(req: GenerateRequest):
    """Create a new job (single or batch) and start it in a background thread."""
    if req.municipality not in DEFAULT_MUNICIPALITIES:
        raise HTTPException(status_code=400, detail="Unknown municipality")

    if not req.all_parcels and not (req.refcat and req.refcat.strip()):
        raise HTTPException(status_code=400, detail="refcat is required when all_parcels=false")

    job = create_job(req.municipality, req.all_parcels, req.refcat.strip() if req.refcat else None)

    t = threading.Thread(target=run_job, args=(job,), daemon=True)
    t.start()

    return GenerateResponse(job_id=job.id)


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> Dict[str, Any]:
    """Return job status, progress, logs, and downloadable files."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    downloadable = []
    for p in job.files:
        path = Path(p)
        downloadable.append(
            {
                "file": path.name,
                "path": str(path),
                "download_url": f"/download/{job.id}/{path.name}",
            }
        )

    return {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "meta": job.meta,
        "logs": job.logs[-500:],
        "files": downloadable,
    }


@app.get("/download/{job_id}/{filename}")
def download_file(job_id: str, filename: str):
    """Download a generated IFC file for a job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    for p in job.files:
        path = Path(p)
        if path.name == filename and path.exists():
            return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")

    raise HTTPException(status_code=404, detail="File not found for this job")

#cd "C:\Users\rauf1\OneDrive\Masaüstü\WORK!\backend"
#..\.venv\Scripts\Activate.ps1
#uvicorn main:app --reload --host 127.0.0.1 --port 8000

