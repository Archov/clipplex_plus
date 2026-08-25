import threading
import time
import unittest

from app.jobs import ClipJobManager, JobFailure, JobQueueFull


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class ClipJobManagerTests(unittest.TestCase):
    def test_payload_is_snapshotted_and_jobs_run_in_queue_order(self):
        received = []

        def worker(payload, progress):
            received.append(payload)
            progress("rendering", 75, 50, "Halfway through rendering.")
            return {"clip": {"file_path": payload["path"]}}

        manager = ClipJobManager(worker, start_worker=False)
        payload = {"path": "first.mp4", "nested": {"start_ms": 123}}
        first = manager.enqueue(payload)
        second = manager.enqueue({"path": "second.mp4"})
        payload["path"] = "changed.mp4"
        payload["nested"]["start_ms"] = 999

        self.assertEqual(manager.get(first)["queue_position"], 1)
        self.assertEqual(manager.get(second)["queue_position"], 2)
        self.assertTrue(manager.run_next())
        self.assertEqual(manager.get(second)["queue_position"], 1)
        self.assertEqual(received[0]["path"], "first.mp4")
        self.assertEqual(received[0]["nested"]["start_ms"], 123)
        self.assertEqual(manager.get(first)["status"], "succeeded")
        self.assertEqual(manager.get(first)["overall_progress"], 100.0)

    def test_queue_limit_counts_waiting_jobs(self):
        manager = ClipJobManager(lambda payload, progress: payload, max_queue=2, start_worker=False)
        manager.enqueue({"number": 1})
        manager.enqueue({"number": 2})

        with self.assertRaises(JobQueueFull):
            manager.enqueue({"number": 3})

    def test_structured_recovery_and_ordinary_failure_states(self):
        def recovery_worker(payload, progress):
            raise JobFailure("recovery_required", {
                "result": "track_selection_required",
                "message": "Choose another subtitle track.",
                "tracks": {"audio": [], "subtitles": []},
            })

        recovery = ClipJobManager(recovery_worker, start_worker=False)
        recovery_id = recovery.enqueue({})
        recovery.run_next()
        status = recovery.get(recovery_id)
        self.assertEqual(status["status"], "recovery_required")
        self.assertEqual(status["error"]["result"], "track_selection_required")

        failed = ClipJobManager(
            lambda payload, progress: (_ for _ in ()).throw(RuntimeError("encoder stopped")),
            start_worker=False,
        )
        failed_id = failed.enqueue({})
        failed.run_next()
        self.assertEqual(failed.get(failed_id)["status"], "failed")
        self.assertEqual(failed.get(failed_id)["error"]["message"], "encoder stopped")

    def test_terminal_jobs_expire_after_retention_period(self):
        clock = FakeClock()
        manager = ClipJobManager(lambda payload, progress: {"ok": True}, retention_seconds=60, start_worker=False, clock=clock)
        job_id = manager.enqueue({})
        manager.run_next()
        self.assertIsNotNone(manager.get(job_id))

        clock.value = 61
        self.assertIsNone(manager.get(job_id))

    def test_daemon_worker_never_runs_two_jobs_at_once(self):
        first_started = threading.Event()
        release_first = threading.Event()
        all_finished = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0
        completed = 0

        def worker(payload, progress):
            nonlocal active, max_active, completed
            with lock:
                active += 1
                max_active = max(max_active, active)
            if payload["number"] == 1:
                first_started.set()
                release_first.wait(timeout=2)
            with lock:
                active -= 1
                completed += 1
                if completed == 2:
                    all_finished.set()
            return payload

        manager = ClipJobManager(worker)
        try:
            first = manager.enqueue({"number": 1})
            self.assertTrue(first_started.wait(timeout=2))
            second = manager.enqueue({"number": 2})
            self.assertEqual(manager.get(first)["status"], "running")
            self.assertEqual(manager.get(second)["queue_position"], 1)
            release_first.set()
            self.assertTrue(all_finished.wait(timeout=2))
            deadline = time.time() + 2
            while manager.get(second)["status"] != "succeeded" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(max_active, 1)
            self.assertEqual(manager.get(second)["status"], "succeeded")
        finally:
            release_first.set()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
