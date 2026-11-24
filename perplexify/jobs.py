"""Background jobs (crawling, indexing, embedding) with progress the UI can poll and cancel."""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from typing import Callable

log = logging.getLogger("perplexify.jobs")


class Cancelled(Exception):
    """Raised inside a job when the user pressed Stop."""


class Job:
    def __init__(self, kind: str, title: str, library_id: str | None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title
        self.library_id = library_id
        self.status = "queued"  # queued | running | done | error | cancelled
        self.phase = ""
        self.current = 0
        self.total = 0
        self.message = ""
        self.error: str | None = None
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.log_lines: list[str] = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def update(self, phase: str | None = None, current: int | None = None, total: int | None = None,
               message: str | None = None) -> None:
        with self._lock:
            if phase is not None:
                if phase != self.phase:
                    self.current, self.total = 0, 0
                self.phase = phase
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total
            if message is not None:
                self.message = message

    def log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line)
            del self.log_lines[:-200]

    def to_dict(self) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "title": self.title, "library_id": self.library_id,
                    "status": self.status, "phase": self.phase, "current": self.current, "total": self.total,
                    "message": self.message, "error": self.error, "created": self.created,
                    "started": self.started, "finished": self.finished, "log": self.log_lines[-30:]}


class JobManager:
    """Runs jobs one at a time (indexing is CPU heavy; running two at once only slows both)."""

    def __init__(self) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._queue: queue.Queue[tuple[Job, Callable[[Job], None]]] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, name="job-worker", daemon=True).start()

    def submit(self, kind: str, title: str, library_id: str | None, fn: Callable[[Job], None]) -> Job:
        job = Job(kind, title, library_id)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > 60:
                self._jobs.popitem(last=False)
        self._queue.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in reversed(self._jobs.values())]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job._cancel.set()
        return True

    def cancel_library(self, library_id: str, timeout: float = 30.0) -> bool:
        """Stop every queued or running job of a library and wait until none is left. True when the library is idle.
        A queued job is cancelled on the spot (the worker may be busy with another library for a long time); a running
        one stops at its next check, normally within a second or two."""
        with self._lock:
            for j in self._jobs.values():
                if j.library_id == library_id and j.status in ("queued", "running"):
                    j._cancel.set()
                    if j.status == "queued":
                        j.status, j.finished, j.message = "cancelled", time.time(), "Stopped."
        deadline = time.time() + timeout
        while library_id in self.active_libraries():
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        return True

    def active_libraries(self) -> set[str]:
        with self._lock:
            return {j.library_id for j in self._jobs.values()
                    if j.library_id and j.status in ("queued", "running")}

    def _worker(self) -> None:
        while True:
            job, fn = self._queue.get()
            with self._lock:          # one step with cancel_library(): a job is either cancelled before it starts or runs
                if job.cancelled:
                    job.status, job.finished = "cancelled", job.finished or time.time()
                    continue
                job.status, job.started = "running", time.time()
            try:
                fn(job)
                job.status = "done"
            except Cancelled:
                job.status = "cancelled"
                job.message = "Stopped."
            except Exception as e:  # noqa: BLE001 - jobs must never kill the worker
                log.exception("job %s failed", job.id)
                job.status, job.error = "error", str(e) or e.__class__.__name__
            finally:
                job.finished = time.time()
