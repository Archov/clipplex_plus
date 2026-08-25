import os
from pathlib import Path
import unittest
from unittest.mock import call, patch

import requests

from app import uploaders


class ResponseStub:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.content = b"json" if payload is not None else b""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class UploaderConfigurationTests(unittest.TestCase):
    def test_only_complete_nonblank_uploaders_are_exposed_without_secrets(self):
        environment = {
            "STREAMABLE_LOGIN": " user@example.com ",
            "STREAMABLE_PASSWORD": " password ",
            "IMMICH_URL": " http://immich:2283 ",
            "IMMICH_API_KEY": " top-secret ",
            "IMMICH_DEFAULT_TAG": " #plex-clip ",
        }
        with patch.dict(os.environ, environment, clear=True):
            configured = uploaders.configured_uploaders()

        self.assertEqual([item["id"] for item in configured], ["streamable", "immich"])
        self.assertEqual(configured[1]["default_tag"], "#plex-clip")
        serialized = repr(configured)
        self.assertNotIn("password", serialized)
        self.assertNotIn("top-secret", serialized)

        with patch.dict(os.environ, {"STREAMABLE_LOGIN": "user", "STREAMABLE_PASSWORD": "   "}, clear=True):
            self.assertEqual(uploaders.configured_uploaders(), [])

    def test_clip_resolution_rejects_files_outside_generated_video_directory(self):
        with self.assertRaises(uploaders.UploadError) as raised:
            uploaders.resolve_clip_path("static/media/videos/../../../../README.md")
        self.assertEqual(raised.exception.status_code, 400)

        with self.assertRaises(uploaders.UploadError) as raised:
            uploaders.resolve_clip_path("static/media/videos/missing.mp4")
        self.assertEqual(raised.exception.status_code, 404)

    def test_streamable_upload_uses_configured_auth_and_returns_link(self):
        clip = uploaders.CLIP_DIRECTORY / "_streamable_test.mp4"
        try:
            clip.write_bytes(b"video")
            with patch.dict(os.environ, {
                "STREAMABLE_LOGIN": "user", "STREAMABLE_PASSWORD": "password"
            }, clear=True), patch("app.uploaders.requests.post") as post:
                post.return_value = ResponseStub({"shortcode": "clip123"})
                result, status = uploaders.streamable_upload(clip)
        finally:
            clip.unlink(missing_ok=True)

        self.assertEqual(status, 200)
        self.assertEqual(result["url"], "https://streamable.com/clip123")
        self.assertEqual(post.call_args.kwargs["auth"], ("user", "password"))

    def test_streamable_upload_requires_both_credentials(self):
        with patch.dict(os.environ, {"STREAMABLE_LOGIN": "user"}, clear=True):
            with self.assertRaises(uploaders.UploadError) as raised:
                uploaders.streamable_upload(Path("unused.mp4"))
        self.assertEqual(raised.exception.status_code, 400)


class ImmichUploaderTests(unittest.TestCase):
    def environment(self, default_tag="#plex-clip"):
        return patch.dict(os.environ, {
            "IMMICH_URL": "http://immich:2283/api/",
            "IMMICH_API_KEY": "secret",
            "IMMICH_DEFAULT_TAG": default_tag,
        }, clear=True)

    def test_normalizes_api_url_and_lists_sorted_options(self):
        with self.environment(), patch.object(uploaders.ImmichUploader, "_request") as request:
            request.side_effect = [
                [{"id": "2", "value": "Zulu"}, {"id": "1", "value": "alpha"}],
                [{"id": "b", "albumName": "Trips"}, {"id": "a", "albumName": "Clips"}],
            ]
            immich = uploaders.ImmichUploader()
            options = immich.options()

        self.assertEqual(immich.api_url, "http://immich:2283/api")
        self.assertEqual(immich.web_url, "http://immich:2283")
        self.assertEqual([tag["name"] for tag in options["tags"]], ["alpha", "Zulu"])
        self.assertEqual([album["name"] for album in options["albums"]], ["Clips", "Trips"])
        self.assertEqual(options["default_tag"], "#plex-clip")

    def test_upload_asset_sends_required_dates_filename_and_api_key(self):
        clip = uploaders.CLIP_DIRECTORY / "_uploader_test.mp4"
        try:
            clip.write_bytes(b"video")
            with self.environment(), patch("app.uploaders.requests.request") as request:
                request.return_value = ResponseStub({"status": "created", "id": "asset-1"}, 201)
                result = uploaders.ImmichUploader()._upload_asset(clip)
        finally:
            clip.unlink(missing_ok=True)

        self.assertEqual(result["id"], "asset-1")
        kwargs = request.call_args.kwargs
        self.assertEqual(request.call_args.args[:2], ("POST", "http://immich:2283/api/assets"))
        self.assertEqual(kwargs["headers"]["x-api-key"], "secret")
        self.assertEqual(kwargs["data"]["filename"], "_uploader_test.mp4")
        self.assertIn("fileCreatedAt", kwargs["data"])
        self.assertIn("fileModifiedAt", kwargs["data"])
        self.assertEqual(kwargs["files"]["assetData"][0], "_uploader_test.mp4")
        self.assertEqual(kwargs["files"]["assetData"][2], "video/mp4")

    def test_default_and_new_tags_are_upserted_and_assigned_once(self):
        with self.environment(), patch.object(uploaders.ImmichUploader, "get_tags") as get_tags, \
             patch.object(uploaders.ImmichUploader, "_request") as request:
            get_tags.return_value = [
                {"id": "existing-id", "name": "existing"},
                {"id": "default-id", "name": "#plex-clip"},
            ]
            request.side_effect = [
                [{"id": "new-id", "value": "new-tag"}],
                {"count": 3},
            ]
            uploaders.ImmichUploader()._assign_tags(
                "asset-1",
                ["existing-id"],
                ["new-tag", "#plex-clip", "new-tag"],
            )

        self.assertEqual(request.call_args_list[0], call("PUT", "/tags", json={"tags": ["new-tag"]}))
        assignment = request.call_args_list[1].kwargs["json"]
        self.assertEqual(assignment["assetIds"], ["asset-1"])
        self.assertEqual(assignment["tagIds"], ["existing-id", "new-id", "default-id"])

    def test_metadata_failures_keep_uploaded_asset_and_report_partial_success(self):
        with self.environment(), patch.object(uploaders.ImmichUploader, "_upload_asset") as upload, \
             patch.object(uploaders.ImmichUploader, "_assign_tags") as tags, \
             patch.object(uploaders.ImmichUploader, "_assign_existing_album") as album, \
             patch.object(uploaders.ImmichUploader, "_create_album") as create_album:
            upload.return_value = {"status": "duplicate", "id": "asset-1"}
            tags.side_effect = uploaders.UploadError("Tag permission denied.")
            album.side_effect = [None, uploaders.UploadError("Album unavailable.")]
            result, status = uploaders.ImmichUploader().upload(
                Path("clip.mp4"), [], [], ["album-1", "album-2"], "New Clips"
            )

        self.assertEqual(status, 207)
        self.assertEqual(result["result"], "partial_success")
        self.assertEqual(result["asset_id"], "asset-1")
        self.assertEqual(result["upload_status"], "duplicate")
        self.assertEqual(len(result["failures"]), 2)
        create_album.assert_called_once_with("asset-1", "New Clips")

    def test_immich_http_error_does_not_expose_api_key(self):
        with self.environment(), patch("app.uploaders.requests.request", return_value=ResponseStub(
            {"message": "Permission denied"}, 403
        )):
            with self.assertRaises(uploaders.UploadError) as raised:
                uploaders.ImmichUploader().get_tags()
        self.assertIn("Permission denied", raised.exception.message)
        self.assertNotIn("secret", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
