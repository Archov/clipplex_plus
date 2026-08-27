import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch
import unittest
import uuid

import ffmpeg

from app import app, clip_library, clip_trims
from app.media_files import (
    MEDIA_DIRECTORY,
    PREVIEW_DIRECTORY,
    VIDEO_DIRECTORY,
    gif_path_for_clip,
    public_media_path,
    thumbnail_path_for_clip,
)


class ClipTrimValidationTests(unittest.TestCase):
    def setUp(self):
        self.database_root = MEDIA_DIRECTORY / f"trim-db-{uuid.uuid4().hex}"
        self.database_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.database_root, True)
        self.database_patch = patch("app.database.DEFAULT_DATABASE_PATH", self.database_root / "clipplex.sqlite3")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        VIDEO_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.clip_path = VIDEO_DIRECTORY / f"trim-validation-{uuid.uuid4().hex}.mp4"
        self.clip_path.write_bytes(b"clip")
        self.probe = patch("app.clip_library.ffmpeg.probe", return_value={
            "format": {"duration": "10", "tags": {"title": "Movie", "comment": "00:01:00.000"}}
        })
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        for path in (
            self.clip_path, gif_path_for_clip(self.clip_path), thumbnail_path_for_clip(self.clip_path),
        ):
            path.unlink(missing_ok=True)
        for path in PREVIEW_DIRECTORY.glob(f"{self.clip_path.stem}-*.mp4"):
            path.unlink(missing_ok=True)

    def payload(self, **updates):
        clip = clip_library.describe_clip(self.clip_path)
        payload = {
            "file_path": clip["file_path"], "expected_revision": clip["revision"],
            "start_ms": 1000, "end_ms": 9000, "basis": "clip", "mode": "new",
        }
        payload.update(updates)
        return payload

    def test_validates_revision_bounds_duration_and_path(self):
        validated = clip_trims.validate_trim_payload(self.payload())
        self.assertEqual(validated["start_ms"], 1000)

        with self.assertRaisesRegex(clip_trims.ClipTrimError, "changed"):
            clip_trims.validate_trim_payload(self.payload(expected_revision="stale"))
        with self.assertRaisesRegex(clip_trims.ClipTrimError, "at least"):
            clip_trims.validate_trim_payload(self.payload(start_ms=1000, end_ms=1050))
        with self.assertRaisesRegex(clip_trims.ClipTrimError, "within"):
            clip_trims.validate_trim_payload(self.payload(end_ms=10001))
        with self.assertRaisesRegex(Exception, "generated Clipplex"):
            clip_trims.validate_trim_payload({**self.payload(), "file_path": "../secret.mp4"})

    def test_private_database_static_access_is_blocked(self):
        client = app.test_client()
        self.assertEqual(client.get("/static/media/.clipplex/clipplex.sqlite3").status_code, 404)

    def test_metadata_edits_preserve_private_source_provenance(self):
        source = {"version": 1, "media_path": "/private/movie.mkv"}
        clip_library.save_clip_metadata(self.clip_path, {
            "media_library": "Movies", "media_type": "movie", "title": "Movie", "year": "",
            "original_start_time": "00:01:00.000", "original_end_time": "00:01:10.000",
            "source": source,
        }, initialize=True)

        clip_library.save_clip_metadata(self.clip_path, {
            "clip_title": "Custom", "media_library": "Movies", "media_type": "movie",
            "title": "Movie", "year": "", "show": "", "season_number": "", "episode_number": "",
        })

        stored = clip_library.load_clip_metadata(self.clip_path)
        self.assertEqual(stored["source"]["version"], source["version"])
        self.assertEqual(stored["source"]["media_path"], source["media_path"])

    def test_ready_source_options_and_preview_results_do_not_expose_source_path(self):
        source = {
            "version": 1, "rating_key": "42", "media_path": str(self.clip_path.resolve()),
            "duration_ms": 120000, "fingerprint": clip_trims._file_fingerprint(self.clip_path),
            "video_stream_index": 0,
            "audio_track": {"id": "audio", "index": 1, "track_type": "audio", "selected": True},
            "subtitle_track": None,
        }
        clip_library.update_clip_fields(public_media_path(self.clip_path), {"source": source})

        options = clip_trims.source_options(public_media_path(self.clip_path))

        self.assertEqual(options["status"], "ready")
        self.assertNotIn(str(self.clip_path), json.dumps(options))
        clip = clip_library.describe_clip(self.clip_path)
        payload = clip_trims.validate_extension_preview_payload({
            "file_path": clip["file_path"], "expected_revision": clip["revision"],
        })
        with self.assertRaisesRegex(clip_trims.ClipTrimError, "replacement Plex source"):
            clip_trims.validate_extension_preview_payload({
                "file_path": clip["file_path"], "expected_revision": clip["revision"], "source_id": "42",
            })

        def fake_render(source_data, clip_data, seek_start, metadata_start, end, output, progress, **kwargs):
            output.write_bytes(b"preview")

        with patch("app.clip_trims._render_from_source", side_effect=fake_render):
            result = clip_trims.run_extension_preview_job(payload, lambda *args: None)

        preview = result["preview"]
        self.assertEqual(preview["window_start_ms"], 30000)
        self.assertEqual(preview["window_end_ms"], 100000)
        self.assertNotIn(str(self.clip_path), json.dumps(result))

    def test_clip_without_saved_source_returns_a_clear_error(self):
        with self.assertRaises(clip_trims.ClipTrimError) as caught:
            clip_trims.source_options(public_media_path(self.clip_path))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("does not have saved original-source metadata", caught.exception.message)
        response = app.test_client().get(
            "/api/clips/source-options", query_string={"file_path": public_media_path(self.clip_path)}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("cannot be extended", response.get_json()["message"])

    def test_deleted_or_changed_saved_source_is_not_matched_again(self):
        original_path = VIDEO_DIRECTORY / f"original-{uuid.uuid4().hex}.mkv"
        original_path.write_bytes(b"original")
        source = {
            "version": 1, "media_path": str(original_path.resolve()),
            "duration_ms": 120000, "fingerprint": clip_trims._file_fingerprint(original_path),
            "video_stream_index": 0,
            "audio_track": {"id": "audio", "index": 1, "track_type": "audio", "selected": True},
            "subtitle_track": None,
        }
        clip_library.update_clip_fields(public_media_path(self.clip_path), {"source": source})

        original_path.unlink()
        with self.assertRaises(clip_trims.ClipTrimError) as deleted:
            clip_trims.source_options(public_media_path(self.clip_path))
        self.assertEqual(deleted.exception.status_code, 422)
        self.assertIn("deleted from Plex", deleted.exception.message)

        original_path.write_bytes(b"changed source")
        try:
            with self.assertRaises(clip_trims.ClipTrimError) as changed:
                clip_trims.source_options(public_media_path(self.clip_path))
            self.assertEqual(changed.exception.status_code, 409)
            self.assertIn("has changed", changed.exception.message)
        finally:
            original_path.unlink(missing_ok=True)


class SyntheticClipTrimTests(unittest.TestCase):
    def setUp(self):
        self.database_root = MEDIA_DIRECTORY / f"trim-synthetic-db-{uuid.uuid4().hex}"
        self.database_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.database_root, True)
        self.database_patch = patch("app.database.DEFAULT_DATABASE_PATH", self.database_root / "clipplex.sqlite3")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        VIDEO_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.clip_path = VIDEO_DIRECTORY / f"trim-synthetic-{uuid.uuid4().hex}.mp4"
        video = ffmpeg.input("color=c=blue:s=320x180:r=24:d=3", f="lavfi")
        audio = ffmpeg.input("sine=frequency=440:sample_rate=48000:duration=3", f="lavfi")
        ffmpeg.output(video.video, audio.audio, str(self.clip_path), vcodec="libx264", acodec="aac", pix_fmt="yuv420p", shortest=None).overwrite_output().run(quiet=True)
        clip_library.save_clip_metadata(self.clip_path, {
            "media_library": "Movies", "media_type": "movie", "title": "Synthetic", "year": "2026",
            "original_start_time": "00:10:00.000", "original_end_time": "00:10:03.000",
        }, initialize=True)

    def tearDown(self):
        for clip in list(VIDEO_DIRECTORY.glob(f"{self.clip_path.stem}*.mp4")):
            for companion in (gif_path_for_clip(clip), thumbnail_path_for_clip(clip)):
                companion.unlink(missing_ok=True)
            clip.unlink(missing_ok=True)

    def test_save_new_renders_exact_range_and_adjusts_absolute_metadata(self):
        clip = clip_library.describe_clip(self.clip_path)
        source_numbers = [
            int(item.get("clip_number") or 0)
            for item in clip_library.list_clips()
            if item.get("source_key") == clip["source_key"]
        ]
        expected_number = max(source_numbers, default=0) + 1
        expected_title = "Synthetic (2026)" + (f" - {expected_number}" if expected_number > 1 else "")
        payload = clip_trims.validate_trim_payload({
            "file_path": clip["file_path"], "expected_revision": clip["revision"],
            "start_ms": 500, "end_ms": 1750, "basis": "clip", "mode": "new",
        })

        result = clip_trims.run_trim_job(payload, lambda *args: None)

        created = result["clip"]
        self.assertEqual(result["operation"], "new")
        self.assertEqual(created["original_start_time"], "00:10:00.500")
        self.assertEqual(created["original_end_time"], "00:10:01.750")
        self.assertEqual(created["clip_title"], expected_title)
        self.assertAlmostEqual(created["duration_ms"], 1250, delta=100)

    def test_failed_replacement_keeps_original_bytes(self):
        original = self.clip_path.read_bytes()
        clip = clip_library.describe_clip(self.clip_path)
        payload = clip_trims.validate_trim_payload({
            "file_path": clip["file_path"], "expected_revision": clip["revision"],
            "start_ms": 500, "end_ms": 1750, "basis": "clip", "mode": "replace",
        })

        with patch("app.clip_trims._render_from_source", side_effect=clip_trims.ClipTrimError("failed", 422)):
            with self.assertRaises(clip_trims.ClipTrimError):
                clip_trims.run_trim_job(payload, lambda *args: None)

        self.assertEqual(self.clip_path.read_bytes(), original)

    def test_successful_replacement_keeps_identity_and_creation_time(self):
        before = clip_library.describe_clip(self.clip_path)
        gif_path_for_clip(self.clip_path).parent.mkdir(parents=True, exist_ok=True)
        gif_path_for_clip(self.clip_path).write_bytes(b"gif")
        payload = clip_trims.validate_trim_payload({
            "file_path": before["file_path"], "expected_revision": before["revision"],
            "start_ms": 250, "end_ms": 2250, "basis": "clip", "mode": "replace",
        })

        result = clip_trims.run_trim_job(payload, lambda *args: None)

        replaced = result["clip"]
        self.assertEqual(replaced["file_path"], before["file_path"])
        self.assertEqual(replaced["clip_title"], before["clip_title"])
        self.assertEqual(replaced["clip_number"], before["clip_number"])
        self.assertEqual(replaced["created_at"], before["created_at"])
        self.assertEqual(replaced["original_start_time"], "00:10:00.250")
        self.assertEqual(replaced["original_end_time"], "00:10:02.250")
        self.assertFalse(gif_path_for_clip(self.clip_path).exists())


if __name__ == "__main__":
    unittest.main()
