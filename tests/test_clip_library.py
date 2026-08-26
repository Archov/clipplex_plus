import json
import os
from pathlib import Path
from unittest.mock import patch
import unittest
import uuid

from app import clip_library
from app.media_files import (
    VIDEO_DIRECTORY,
    metadata_path_for_clip,
    public_media_path,
    thumbnail_path_for_clip,
)


class ClipLibraryTests(unittest.TestCase):
    def setUp(self):
        VIDEO_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.clip_path = VIDEO_DIRECTORY / f"library-test-{uuid.uuid4().hex}.mp4"
        self.clip_path.write_bytes(b"clip")
        self.probe = patch("app.clip_library.ffmpeg.probe", return_value={
            "format": {"duration": "12.5", "tags": {
                "title": "The Adventure",
                "show": "Sample Show",
                "season_number": "1",
                "episode_id": "3",
                "comment": "00:01:02.500",
                "artist": "alice",
            }}
        })
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        for path in (
            self.clip_path,
            metadata_path_for_clip(self.clip_path),
            thumbnail_path_for_clip(self.clip_path),
        ):
            path.unlink(missing_ok=True)

    def test_legacy_episode_is_inferred_and_uncategorized(self):
        clip = clip_library.describe_clip(self.clip_path)

        self.assertEqual(clip["media_type"], "episode")
        self.assertEqual(clip["media_library"], "Uncategorized")
        self.assertEqual(clip["episode_code"], "S01E03")
        self.assertEqual(clip["display_heading"], "Sample Show · S01E03")
        self.assertEqual(clip["display_subtitle"], "The Adventure")
        self.assertEqual(clip["original_start_time"], "00:01:02.500")
        self.assertEqual(clip["original_end_time"], "00:01:15.000")
        self.assertEqual(clip["duration_ms"], 12500)

    def test_edit_details_persists_without_rewriting_video(self):
        original_mtime = self.clip_path.stat().st_mtime_ns

        clip = clip_library.save_clip_metadata(public_media_path(self.clip_path), {
            "media_library": "Movies",
            "media_type": "movie",
            "title": "King Kong",
            "year": "1933",
            "show": "Ignored Show",
            "season_number": "1",
            "episode_number": "2",
        })

        self.assertEqual(clip["display_heading"], "King Kong (1933)")
        self.assertEqual(clip["clip_title"], "King Kong (1933)")
        self.assertEqual(clip["show"], "")
        self.assertEqual(self.clip_path.stat().st_mtime_ns, original_mtime)
        sidecar = json.loads(metadata_path_for_clip(self.clip_path).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["media_library"], "Movies")
        self.assertEqual(sidecar["year"], "1933")
        self.assertEqual(sidecar["original_end_time"], "00:01:15.000")

    def test_clip_title_can_be_customized_and_reset_to_inferred_title(self):
        custom = clip_library.save_clip_metadata(public_media_path(self.clip_path), {
            "clip_title": "The best part",
            "media_library": "TV Shows",
            "media_type": "episode",
            "title": "The Adventure",
            "show": "Sample Show",
            "season_number": "1",
            "episode_number": "3",
            "year": "",
        })

        self.assertEqual(custom["clip_title"], "The best part")
        self.assertTrue(custom["clip_title_custom"])

        inferred = clip_library.save_clip_metadata(public_media_path(self.clip_path), {
            "clip_title": "",
            "media_library": "TV Shows",
            "media_type": "episode",
            "title": "A New Title",
            "show": "Sample Show",
            "season_number": "1",
            "episode_number": "3",
            "year": "",
        })

        self.assertEqual(inferred["clip_title"], "Sample Show - S01E03 - A New Title")
        self.assertFalse(inferred["clip_title_custom"])

    def test_legacy_clip_without_start_time_uses_zero_then_duration(self):
        with patch("app.clip_library.ffmpeg.probe", return_value={"format": {"duration": "2.25", "tags": {"title": "Movie"}}}):
            clip = clip_library.describe_clip(self.clip_path)

        self.assertEqual(clip["original_start_time"], "00:00:00.000")
        self.assertEqual(clip["original_end_time"], "00:00:02.250")

    def test_invalid_year_and_traversal_are_rejected(self):
        with self.assertRaisesRegex(Exception, "four digits"):
            clip_library.save_clip_metadata(public_media_path(self.clip_path), {
                "media_library": "Movies", "media_type": "movie",
                "title": "Movie", "year": "33",
            })
        with self.assertRaisesRegex(Exception, "generated Clipplex"):
            clip_library.ensure_thumbnail("../secret.mp4")

    def test_list_is_newest_first(self):
        older_path = VIDEO_DIRECTORY / f"library-test-{uuid.uuid4().hex}.mp4"
        older_path.write_bytes(b"older")
        try:
            now = self.clip_path.stat().st_mtime
            os.utime(older_path, (now - 60, now - 60))
            paths = [clip["file_path"] for clip in clip_library.list_clips()]
            self.assertLess(paths.index(public_media_path(self.clip_path)), paths.index(public_media_path(older_path)))
        finally:
            older_path.unlink(missing_ok=True)
            metadata_path_for_clip(older_path).unlink(missing_ok=True)

    def test_clips_from_the_same_source_receive_stable_numbered_titles(self):
        older_path = VIDEO_DIRECTORY / f"library-test-{uuid.uuid4().hex}.mp4"
        older_path.write_bytes(b"older")
        try:
            now = self.clip_path.stat().st_mtime
            os.utime(older_path, (now - 60, now - 60))

            clips = {
                clip["file_path"]: clip
                for clip in clip_library.list_clips()
                if clip["file_path"] in {public_media_path(self.clip_path), public_media_path(older_path)}
            }

            self.assertEqual(clips[public_media_path(older_path)]["clip_title"], "Sample Show - S01E03 - The Adventure")
            self.assertEqual(clips[public_media_path(self.clip_path)]["clip_title"], "Sample Show - S01E03 - The Adventure - 2")
            self.assertEqual(clips[public_media_path(self.clip_path)]["clip_number"], 2)

            older_path.unlink()
            metadata_path_for_clip(older_path).unlink(missing_ok=True)
            remaining = clip_library.list_clips()
            current = next(clip for clip in remaining if clip["file_path"] == public_media_path(self.clip_path))
            self.assertEqual(current["clip_title"], "Sample Show - S01E03 - The Adventure - 2")
        finally:
            older_path.unlink(missing_ok=True)
            metadata_path_for_clip(older_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
