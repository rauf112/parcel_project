from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import time
import uuid

@dataclass
class Job:
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

JOBS: Dict[str, Job] = {}

def create_job(municipality: str, all_parcels: bool, refcat: Optional[str]) -> Job:
    jid = uuid.uuid4().hex
    job = Job(id=jid, municipality=municipality, all_parcels=all_parcels, refcat=refcat)
    JOBS[jid] = job
    return job

def get_job(job_id: str) -> Optional[Job]:
    return JOBS.get(job_id)

def append_log(job: Job, line: str) -> None:
    job.logs.append(line)
    job.message = line
