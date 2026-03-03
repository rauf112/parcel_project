"""
In-memory job registry for envelope generation.

Responsibilities
----------------
- Stores all job objects in memory (no persistence).
- Provides lightweight helpers to create jobs, fetch jobs, and append logs.
- Used by the API layer and background workers to track progress and results.

Notes
-----
- All state is lost on process restart.
- Thread-safety is minimal; callers should avoid heavy concurrent writes.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import time
import uuid

@dataclass
class Job:
    """
    Represents a single envelope generation job.

    Fields
    ------
    id: Unique job identifier (UUID4 hex).
    municipality: Municipality name.
    all_parcels: True for batch jobs; False for a single parcel.
    refcat: Cadastral reference code (only for single-parcel jobs).
    status: Job state (queued | running | success | error).
    progress: Completion ratio in [0.0, 1.0].
    message: Last status or error message.
    created_at/started_at/finished_at: Timestamps (epoch seconds).
    files: Output file paths produced by the job.
    logs: Append-only log lines for UI or diagnostics.
    """
    id: str
    municipality: str
    all_parcels: bool
    refcat: Optional[str]
    status: str = "queued"          # queued | running | success | error
    progress: float = 0.0           # 0..1
    message: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    files: List[str] = field(default_factory=list)   # absolute paths
    logs: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

JOBS: Dict[str, Job] = {}

def create_job(municipality: str, all_parcels: bool, refcat: Optional[str]) -> Job:
    """
    Create a new Job, assign a unique id, and store it in the in-memory registry.

    Parameters
    ----------
    municipality: Name of the municipality.
    all_parcels: True for batch jobs; False for a single parcel.
    refcat: Parcel reference code for single jobs (can be None for batch).
    """
    jid = uuid.uuid4().hex
    job = Job(id=jid, municipality=municipality, all_parcels=all_parcels, refcat=refcat)
    JOBS[jid] = job
    return job

def get_job(job_id: str) -> Optional[Job]:
    """
    Return the Job by id, or None if it does not exist.
    """
    return JOBS.get(job_id)

def append_log(job: Job, line: str) -> None:
    """
    Append a log line to the job and update the last message.
    """
    job.logs.append(line)
    job.message = line
