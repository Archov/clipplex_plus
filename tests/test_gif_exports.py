import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

import ffmpeg

from app import gif_exports
from app.media_files import GIF_DIRECTORY, VIDEO_DIRECTORY, delete_generated_clip


class GifExportTests(unittest.TestCase):
    def setUp(self):
        VIDEO_DIRECTORY.mkdir(parents=True, exist_ok=True)
        GIF_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.clip = VIDEO_DIRECTORY / "_gif_export_test.mp4"
        self.gif = GIF_DIRECTORY / "_gif_export_test.gif"
        self.clip.write_bytes(b"mp4")

    def tearDown(self):
        self.clip.unlink(missing_ok=True)
        self.gif.unlink(missing_ok=True)
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

        delete_generated_clip(self.public_clip_path)

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
