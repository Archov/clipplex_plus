import os
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch

import ffmpeg

from app import gif_exports
from app.media_files import (
    CLIP_METADATA_DIRECTORY,
    GIF_DIRECTORY,
    THUMBNAIL_DIRECTORY,
    VIDEO_DIRECTORY,
    delete_generated_clip,
    metadata_path_for_clip,
    thumbnail_path_for_clip,
)


class GifExportTests(unittest.TestCase):
    def setUp(self):
        VIDEO_DIRECTORY.mkdir(parents=True, exist_ok=True)
        GIF_DIRECTORY.mkdir(parents=True, exist_ok=True)
        THUMBNAIL_DIRECTORY.mkdir(parents=True, exist_ok=True)
        CLIP_METADATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.clip = VIDEO_DIRECTORY / "_gif_export_test.mp4"
        self.gif = GIF_DIRECTORY / "_gif_export_test.gif"
        self.thumbnail = thumbnail_path_for_clip(self.clip)
        self.metadata = metadata_path_for_clip(self.clip)
        self.clip.write_bytes(b"mp4")

    def tearDown(self):
        self.clip.unlink(missing_ok=True)
        self.gif.unlink(missing_ok=True)
        self.thumbnail.unlink(missing_ok=True)
        self.metadata.unlink(missing_ok=True)
        for temporary in GIF_DIRECTORY.glob("._gif_export_test.*.tmp.gif"):
            temporary.unlink(missing_ok=True)

    @property
    def public_clip_path(self):
        return "static/media/videos/_gif_export_test.mp4"

    def test_rejects_traversal_missing_and_non_mp4_sources(self):
        with self.assertRaises(gif_exports.GifExportError) as traversal:
            gif_exports.cached_export("static/media/videos/../../../../README.md")
        self.assertEqual(traversal.exception.status_code, 400)

        with self.assertRaises(gif_exports.GifExportError) as missing:
            gif_exports.cached_export("static/media/videos/missing.mp4")
        self.assertEqual(missing.exception.status_code, 404)

        text_file = VIDEO_DIRECTORY / "_gif_export_test.txt"
        text_file.write_text("not a clip", encoding="utf-8")
        try:
            with self.assertRaises(gif_exports.GifExportError) as wrong_type:
                gif_exports.cached_export("static/media/videos/_gif_export_test.txt")
            self.assertEqual(wrong_type.exception.status_code, 400)
        finally:
            text_file.unlink(missing_ok=True)

    def test_cache_must_be_fresh_nonempty_and_under_the_size_limit(self):
        self.gif.write_bytes(b"gif")
        now = time.time()
        os.utime(self.clip, (now, now))
        os.utime(self.gif, (now + 1, now + 1))
        self.assertIsNotNone(gif_exports.cached_export(self.public_clip_path))

        os.utime(self.clip, (now + 2, now + 2))
        self.assertIsNone(gif_exports.cached_export(self.public_clip_path))

        os.utime(self.gif, (now + 3, now + 3))
        with patch.object(gif_exports, "MAX_GIF_BYTES", 2):
            self.assertIsNone(gif_exports.cached_export(self.public_clip_path))

    def test_disappearing_cached_gif_is_treated_as_a_cache_miss(self):
        self.gif.write_bytes(b"gif")

        with patch.object(gif_exports, "_is_valid_cache", return_value=True), \
                patch.object(gif_exports, "_descriptor", side_effect=FileNotFoundError):
            self.assertIsNone(gif_exports.cached_export(self.public_clip_path))

    def test_falls_back_profiles_and_atomically_publishes_first_small_result(self):
        attempts = []

        def graph(_clip, output, profile):
            attempts.append(profile)
            return output

        def render(output, _duration, callback):
            Path(output).write_bytes(b"too-large" if len(attempts) == 1 else b"small")
            callback(100)

        with patch.object(gif_exports, "MAX_GIF_BYTES", 6), \
                patch.object(gif_exports, "_duration_seconds", return_value=1), \
                patch.object(gif_exports, "build_gif_graph", side_effect=graph), \
                patch.object(gif_exports.clipplexAPI, "run_ffmpeg_with_progress", side_effect=render):
            result = gif_exports.export_gif(self.public_clip_path)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.gif.read_bytes(), b"small")
        self.assertFalse(result["export"]["cached"])
        self.assertEqual(result["export"]["size_bytes"], 5)

    def test_oversized_attempts_leave_no_published_or_temporary_file(self):
        def graph(_clip, output, _profile):
            return output

        def render(output, _duration, _callback):
            Path(output).write_bytes(b"too-large")

        with patch.object(gif_exports, "MAX_GIF_BYTES", 2), \
                patch.object(gif_exports, "_duration_seconds", return_value=1), \
                patch.object(gif_exports, "build_gif_graph", side_effect=graph), \
                patch.object(gif_exports.clipplexAPI, "run_ffmpeg_with_progress", side_effect=render):
            with self.assertRaises(gif_exports.GifExportError) as raised:
                gif_exports.export_gif(self.public_clip_path)

        self.assertIn("shorter clip", raised.exception.message)
        self.assertFalse(self.gif.exists())
        self.assertEqual(list(GIF_DIRECTORY.glob("._gif_export_test.*.tmp.gif")), [])

    def test_deleted_source_during_render_does_not_publish_an_orphan(self):
        def graph(_clip, output, _profile):
            return output

        def render(output, _duration, _callback):
            Path(output).write_bytes(b"gif")
            self.clip.unlink()

        with patch.object(gif_exports, "_duration_seconds", return_value=1), \
                patch.object(gif_exports, "build_gif_graph", side_effect=graph), \
                patch.object(gif_exports.clipplexAPI, "run_ffmpeg_with_progress", side_effect=render):
            with self.assertRaises(gif_exports.GifExportError) as raised:
                gif_exports.export_gif(self.public_clip_path)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertFalse(self.gif.exists())

    def test_ffmpeg_graph_is_silent_looping_palette_gif_with_bounded_scaling(self):
        command = " ".join(ffmpeg.compile(gif_exports.build_gif_graph(
            Path("clip.mp4"), Path("clip.gif"), gif_exports.GIF_PROFILES[0]
        )))

        self.assertIn("fps=fps=15", command)
        self.assertIn("scale=min(720", command)
        self.assertIn("force_original_aspect_ratio=decrease", command)
        self.assertIn("palettegen=max_colors=128", command)
        self.assertIn("paletteuse=", command)
        self.assertIn("-loop 0", command)
        self.assertNotIn("0:a", command)

    def test_deleting_clip_also_deletes_cached_gif(self):
        self.gif.write_bytes(b"gif")
        self.thumbnail.write_bytes(b"jpg")
        self.metadata.write_text("{}", encoding="utf-8")

        delete_generated_clip(self.public_clip_path)

        self.assertFalse(self.clip.exists())
        self.assertFalse(self.gif.exists())
        self.assertFalse(self.thumbnail.exists())
        self.assertFalse(self.metadata.exists())

    def test_cached_gif_cleanup_failure_does_not_prevent_clip_deletion(self):
        self.gif.write_bytes(b"gif")
        real_unlink = Path.unlink

        def fail_gif_unlink(path, missing_ok=False):
            if path == self.gif:
                raise PermissionError("GIF is locked")
            return real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", new=fail_gif_unlink):
            delete_generated_clip(self.public_clip_path)

        self.assertFalse(self.clip.exists())
        self.assertTrue(self.gif.exists())

    def test_clip_deletion_cannot_race_with_final_gif_publication(self):
        replace_started = threading.Event()
        allow_replace = threading.Event()
        delete_started = threading.Event()
        delete_finished = threading.Event()
        export_errors = []
        delete_errors = []
        real_replace = os.replace

        def graph(_clip, output, _profile):
            return output

        def render(output, _duration, callback):
            Path(output).write_bytes(b"gif")
            callback(100)

        def blocking_replace(source, destination):
            replace_started.set()
            allow_replace.wait(timeout=2)
            real_replace(source, destination)

        def run_export():
            try:
                gif_exports.export_gif(self.public_clip_path)
            except Exception as error:
                export_errors.append(error)

        def run_delete():
            delete_started.set()
            try:
                delete_generated_clip(self.public_clip_path)
            except Exception as error:
                delete_errors.append(error)
            finally:
                delete_finished.set()

        with patch.object(gif_exports, "_duration_seconds", return_value=1), \
                patch.object(gif_exports, "build_gif_graph", side_effect=graph), \
                patch.object(gif_exports.clipplexAPI, "run_ffmpeg_with_progress", side_effect=render), \
                patch.object(gif_exports.os, "replace", side_effect=blocking_replace):
            exporter = threading.Thread(target=run_export)
            exporter.start()
            self.assertTrue(replace_started.wait(timeout=2))

            deleter = threading.Thread(target=run_delete)
            deleter.start()
            self.assertTrue(delete_started.wait(timeout=2))
            self.assertFalse(delete_finished.wait(timeout=0.05))

            allow_replace.set()
            exporter.join(timeout=2)
            deleter.join(timeout=2)

        self.assertFalse(exporter.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual(export_errors, [])
        self.assertEqual(delete_errors, [])
        self.assertFalse(self.clip.exists())
        self.assertFalse(self.gif.exists())

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and FFprobe are required for GIF integration coverage",
    )
    def test_synthetic_video_exports_as_an_animated_gif_under_the_limit(self):
        source = ffmpeg.input("testsrc2=size=320x180:rate=12:duration=1", f="lavfi")
        (
            ffmpeg.output(source, str(self.clip), vcodec="libx264", pix_fmt="yuv420p")
            .overwrite_output()
            .run(quiet=True)
        )

        result = gif_exports.export_gif(self.public_clip_path)
        probe = ffmpeg.probe(str(self.gif), select_streams="v:0")

        self.assertLessEqual(result["export"]["size_bytes"], gif_exports.MAX_GIF_BYTES)
        self.assertGreater(int(probe["streams"][0].get("nb_frames") or 0), 1)


if __name__ == "__main__":
    unittest.main()
