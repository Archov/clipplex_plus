import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
from unittest.mock import patch
import unittest
import uuid

import ffmpeg

from app import clip_library
from app.media_files import (
    MEDIA_DIRECTORY,
    legacy_metadata_path_for_clip,
    metadata_path_for_clip,
    public_media_path,
    thumbnail_path_for_clip,
)


class ClipLibraryTests(unittest.TestCase):
    def setUp(self):
        test_root = MEDIA_DIRECTORY / f"clip-library-tests-{uuid.uuid4().hex}"
        test_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, test_root, True)
        self.video_directory = test_root / "videos"
        self.metadata_directory = test_root / "metadata"
        self.thumbnail_directory = test_root / "thumbnails"
        self.directory_patches = [
            patch("app.database.DEFAULT_DATABASE_PATH", test_root / "clipplex.sqlite3"),
            patch("app.clip_library.VIDEO_DIRECTORY", self.video_directory),
            patch("app.clip_library.THUMBNAIL_DIRECTORY", self.thumbnail_directory),
            patch("app.media_files.VIDEO_DIRECTORY", self.video_directory),
            patch("app.media_files.CLIP_METADATA_DIRECTORY", self.metadata_directory),
            patch("app.media_files.THUMBNAIL_DIRECTORY", self.thumbnail_directory),
        ]
        for directory_patch in self.directory_patches:
            directory_patch.start()
            self.addCleanup(directory_patch.stop)
        self.video_directory.mkdir(parents=True, exist_ok=True)
        self.metadata_directory.mkdir(parents=True, exist_ok=True)
        self.thumbnail_directory.mkdir(parents=True, exist_ok=True)
        self.clip_path = self.video_directory / f"library-test-{uuid.uuid4().hex}.mp4"
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
        stored = clip_library._read_sidecar(self.clip_path)
        self.assertEqual(stored["media_library"], "Movies")
        self.assertEqual(stored["year"], "1933")
        self.assertEqual(stored["original_end_time"], "00:01:15.000")
        self.assertFalse(metadata_path_for_clip(self.clip_path).exists())

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
        older_path = self.video_directory / f"library-test-{uuid.uuid4().hex}.mp4"
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
        older_path = self.video_directory / f"library-test-{uuid.uuid4().hex}.mp4"
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

    def test_title_allocation_uses_sidecars_without_listing_or_probing(self):
        clip_library.describe_clip(self.clip_path)
        with patch("app.clip_library.list_clips") as listing, patch("app.clip_library.ffmpeg.probe") as probe:
            allocated = clip_library.allocate_clip_title({
                "media_library": "TV Shows", "media_type": "episode", "title": "The Adventure",
                "show": "Sample Show", "season_number": "1", "episode_number": "3", "year": "",
            })

        self.assertEqual(allocated["clip_number"], 1)
        listing.assert_not_called()
        probe.assert_not_called()

    def test_title_allocation_reads_legacy_sidecars_without_migrating_them(self):
        metadata = {
            "media_library": "TV Shows", "media_type": "episode", "title": "The Adventure",
            "show": "Sample Show", "season_number": "1", "episode_number": "3", "year": "",
        }
        legacy_path = legacy_metadata_path_for_clip(self.clip_path)
        legacy_path.write_text(json.dumps({
            **metadata,
            "source_key": clip_library._source_key(metadata),
            "clip_number": 3,
        }), encoding="utf-8")

        with patch("app.clip_library.os.replace") as replace:
            allocated = clip_library.allocate_clip_title(metadata)

        self.assertEqual(allocated["clip_number"], 4)
        replace.assert_not_called()
        self.assertFalse(legacy_path.is_file())

    def test_initialize_reuses_a_supplied_clip_identity(self):
        metadata = {
            "media_library": "TV Shows", "media_type": "episode", "title": "The Adventure",
            "show": "Sample Show", "season_number": "1", "episode_number": "3", "year": "",
        }
        supplied = {
            "source_key": clip_library._source_key(metadata), "clip_number": 7,
            "clip_title": "Supplied title - 7", "clip_title_custom": False,
        }
        with patch("app.clip_library.allocate_clip_title") as allocate:
            clip = clip_library.save_clip_metadata(self.clip_path, {
                **metadata,
                **supplied,
            }, initialize=True)

        allocate.assert_not_called()
        self.assertEqual(clip["source_key"], supplied["source_key"])
        self.assertEqual(clip["clip_number"], 7)
        self.assertEqual(clip["clip_title"], "Supplied title - 7")

    def test_non_persisting_listing_never_writes_numbering_sidecars(self):
        with patch("app.clip_library._update_sidecar") as update:
            clip_library.list_clips(persist=False)

        update.assert_not_called()

    def test_analysis_is_cached_until_the_file_fingerprint_changes(self):
        clip_library.describe_clip(self.clip_path)
        self.probe.stop()
        cached_probe = patch("app.clip_library.ffmpeg.probe", return_value={
            "format": {"duration": "20", "tags": {"title": "Changed"}}
        })
        probe = cached_probe.start()
        try:
            self.assertEqual(clip_library.describe_clip(self.clip_path)["duration_ms"], 12500)
            probe.assert_not_called()
            self.clip_path.write_bytes(b"changed clip bytes")
            self.assertEqual(clip_library.describe_clip(self.clip_path)["duration_ms"], 20000)
            probe.assert_called_once()
        finally:
            cached_probe.stop()
            self.probe.start()

    def test_failed_analysis_is_cached_until_the_file_fingerprint_changes(self):
        self.probe.stop()
        failed_probe = patch(
            "app.clip_library.ffmpeg.probe",
            side_effect=ffmpeg.Error("ffprobe", b"", b"probe failed"),
        )
        probe = failed_probe.start()
        try:
            self.assertEqual(clip_library.describe_clip(self.clip_path)["duration_ms"], 0)
            self.assertEqual(clip_library.describe_clip(self.clip_path)["duration_ms"], 0)
            probe.assert_called_once()
            self.clip_path.write_bytes(b"changed after failed probe")
            clip_library.describe_clip(self.clip_path)
            self.assertEqual(probe.call_count, 2)
        finally:
            failed_probe.stop()
            self.probe.start()

    def test_malformed_sidecar_is_retained_and_retried_after_repair(self):
        sidecar = metadata_path_for_clip(self.clip_path)
        sidecar.write_text("{not-json", encoding="utf-8")

        first = clip_library.describe_clip(self.clip_path)
        self.assertEqual(first["title"], "The Adventure")
        self.assertTrue(sidecar.exists())

        sidecar.write_text(json.dumps({"title": "Repaired", "media_library": "Recovered"}), encoding="utf-8")
        repaired = clip_library.describe_clip(self.clip_path)
        self.assertEqual(repaired["title"], "Repaired")
        self.assertEqual(repaired["media_library"], "Recovered")
        self.assertFalse(sidecar.exists())

    def test_sidecar_is_not_deleted_when_database_import_fails(self):
        sidecar = metadata_path_for_clip(self.clip_path)
        sidecar.write_text(json.dumps({"title": "Keep me"}), encoding="utf-8")

        with patch("app.clip_library._insert_new_clip", side_effect=OSError("database unavailable")):
            with self.assertRaisesRegex(OSError, "database unavailable"):
                clip_library.describe_clip(self.clip_path)

        self.assertTrue(sidecar.exists())

    def test_database_sort_modes_order_clips(self):
        second = self.video_directory / f"library-test-{uuid.uuid4().hex}.mp4"
        second.write_bytes(b"second")
        try:
            clip_library.save_clip_metadata(self.clip_path, {
                "media_library": "Movies", "media_type": "movie", "title": "Zulu", "year": "",
                "created_at": "2024-01-02T00:00:00Z",
            }, initialize=True)
            with patch("app.clip_library.ffmpeg.probe", return_value={"format": {"duration": "2", "tags": {}}}):
                clip_library.save_clip_metadata(second, {
                    "media_library": "Movies", "media_type": "movie", "title": "Alpha", "year": "",
                    "created_at": "2024-01-01T00:00:00Z",
                }, initialize=True)
            expected = {
                "newest": ["Zulu", "Alpha"], "oldest": ["Alpha", "Zulu"],
                "title_asc": ["Alpha", "Zulu"], "title_desc": ["Zulu", "Alpha"],
                "duration_asc": ["Alpha", "Zulu"], "duration_desc": ["Zulu", "Alpha"],
            }
            for sort, titles in expected.items():
                with self.subTest(sort=sort):
                    self.assertEqual([clip["title"] for clip in clip_library.list_clips(sort=sort)], titles)
            with self.assertRaisesRegex(Exception, "Unsupported"):
                clip_library.list_clips(sort="arbitrary")
        finally:
            second.unlink(missing_ok=True)

    def test_concurrent_initialization_keeps_source_numbers_unique(self):
        second = self.video_directory / f"library-test-{uuid.uuid4().hex}.mp4"
        second.write_bytes(b"second")
        metadata = {
            "media_library": "TV Shows", "media_type": "episode", "title": "The Adventure",
            "show": "Sample Show", "season_number": "1", "episode_number": "3", "year": "",
        }
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                clips = list(executor.map(
                    lambda path: clip_library.save_clip_metadata(path, metadata, initialize=True),
                    (self.clip_path, second),
                ))
            self.assertEqual({clip["clip_number"] for clip in clips}, {1, 2})
        finally:
            second.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
