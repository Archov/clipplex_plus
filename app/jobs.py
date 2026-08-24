from collections import deque
from copy import deepcopy
import logging
import threading
import time
import uuid


LOGGER = logging.getLogger(__name__)


class JobQueueFull(Exception):
    pass


class JobFailure(Exception):
    def __init__(self, status, error):
        super().__init__(error.get("message") or "Clip creation failed.")
        self.status = status
        self.error = deepcopy(error)


class ClipJobManager:
    def __init__(self, worker, max_queue=10, retention_seconds=3600, start_worker=True, clock=time.time):
        self.worker = worker
        self.max_queue = max_queue
        self.retention_seconds = retention_seconds
        self.clock = clock
        self.jobs = {}
        self.queue = deque()
        self.condition = threading.Condition(threading.RLock())
        self.stopped = False
        self.thread = None
        if start_worker:
            self.thread = threading.Thread(target=self._worker_loop, name="clipplex-job-worker", daemon=True)
            self.thread.start()

    def enqueue(self, payload):
        with self.condition:
            self._prune_locked()
            if len(self.queue) >= self.max_queue:
                raise JobQueueFull("The clip queue is full. Try again after another clip finishes.")
            now = self.clock()
            job_id = str(uuid.uuid4())
            self.jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "stage": "queued",
                "message": "Waiting for the renderer.",
                "overall_progress": 0.0,
                "stage_progress": 0.0,
                "payload": deepcopy(payload),
                "result": None,
                "error": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
            }
            self.queue.append(job_id)
            self.condition.notify()
            return job_id

    def get(self, job_id):
        with self.condition:
            self._prune_locked()
            job = self.jobs.get(job_id)
            if job is None:
                return None
            now = self.clock()
            started = job["created_at"]
            ended = job["finished_at"] or now
            queue_position = None
            if job["status"] == "queued":
                try:
                    queue_position = list(self.queue).index(job_id) + 1
                except ValueError:
                    queue_position = None
            return {
                "job_id": job_id,
                "status": job["status"],
                "stage": job["stage"],
                "message": job["message"],
                "overall_progress": round(job["overall_progress"], 1),
                "stage_progress": round(job["stage_progress"], 1),
                "elapsed_ms": max(0, int((ended - started) * 1000)),
                "queue_position": queue_position,
                "result": deepcopy(job["result"]),
                "error": deepcopy(job["error"]),
            }

    def update(self, job_id, stage, overall_progress, stage_progress, message):
        with self.condition:
            job = self.jobs.get(job_id)
            if job is None or job["status"] != "running":
                return
            job["stage"] = stage
            job["message"] = message
            job["overall_progress"] = max(
                job["overall_progress"], min(99.0, max(0.0, float(overall_progress)))
            )
            job["stage_progress"] = min(100.0, max(0.0, float(stage_progress)))

    def run_next(self):
        with self.condition:
            if not self.queue:
                return False
            job_id = self.queue.popleft()
            job = self.jobs.get(job_id)
            if job is None:
                return False
            job["status"] = "running"
            job["stage"] = "starting"
            job["message"] = "Starting clip creation."
            job["overall_progress"] = 1.0
            job["stage_progress"] = 0.0
            job["started_at"] = self.clock()
            payload = deepcopy(job["payload"])

        def progress(stage, overall_progress, stage_progress, message):
            self.update(job_id, stage, overall_progress, stage_progress, message)

        try:
            result = self.worker(payload, progress)
        except JobFailure as error:
            with self.condition:
                job = self.jobs[job_id]
                job["status"] = error.status
                job["stage"] = error.status
                job["message"] = error.error.get("message") or str(error)
                job["error"] = deepcopy(error.error)
                job["finished_at"] = self.clock()
        except Exception as error:
            LOGGER.exception("Unhandled clip job failure")
            with self.condition:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["stage"] = "failed"
                job["message"] = str(error) or "Clipplex could not create the clip."
                job["error"] = {"result": "error", "message": job["message"]}
                job["finished_at"] = self.clock()
        else:
            with self.condition:
                job = self.jobs[job_id]
                job["status"] = "succeeded"
                job["stage"] = "complete"
                job["message"] = "Clip created."
                job["overall_progress"] = 100.0
                job["stage_progress"] = 100.0
                job["result"] = deepcopy(result)
                job["finished_at"] = self.clock()
        return True

    def _worker_loop(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or bool(self.queue))
                if self.stopped:
                    return
            self.run_next()

    def _prune_locked(self):
        cutoff = self.clock() - self.retention_seconds
        expired = [
            job_id for job_id, job in self.jobs.items()
            if job["finished_at"] is not None and job["finished_at"] < cutoff
        ]
        for job_id in expired:
            self.jobs.pop(job_id, None)

    def shutdown(self, timeout=1):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
