import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import call, patch

import requests

from app import uploaders
from app.media_files import MEDIA_DIRECTORY


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
    def setUp(self):
        self.temporary_directory = MEDIA_DIRECTORY / f"uploader-db-{os.urandom(8).hex()}"
        self.temporary_directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.temporary_directory, True)
        self.database_patch = patch(
            "app.database.DEFAULT_DATABASE_PATH",
            self.temporary_directory / "clipplex.sqlite3",
        )
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)

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
            retained = uploaders.configured_uploaders()
        self.assertEqual([item["id"] for item in retained], ["streamable", "immich"])

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
    def setUp(self):
        self.database_root = MEDIA_DIRECTORY / f"immich-db-{os.urandom(8).hex()}"
        self.database_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.database_root, True)
        self.database_patch = patch("app.database.DEFAULT_DATABASE_PATH", self.database_root / "clipplex.sqlite3")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)

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

    def test_asset_url_only_requires_the_configured_immich_url(self):
        with patch("app.uploaders._setting", side_effect=lambda key: "http://immich:2283/api/" if key == "immich_url" else ""):
            self.assertEqual(uploaders.immich_asset_url("asset-1"), "http://immich:2283/photos/asset-1")

    def test_api_key_introspection_uses_current_key_endpoint_and_redacts_response(self):
        with self.environment(), patch("app.uploaders.requests.request") as request:
            request.return_value = ResponseStub({
                "id": "key-id",
                "name": "Clipplex",
                "permissions": ["tag.read", "asset.read", "tag.read"],
            })
            result = uploaders.ImmichUploader().get_api_key()

        self.assertEqual(result, {
            "name": "Clipplex",
            "permissions": ["asset.read", "tag.read"],
        })
        self.assertNotIn("id", result)
        self.assertEqual(
            request.call_args.args[:2],
            ("GET", "http://immich:2283/api/api-keys/me"),
        )
        self.assertEqual(request.call_args.kwargs["headers"]["x-api-key"], "secret")

    def test_api_key_introspection_rejects_malformed_response(self):
        with self.environment(), patch.object(
            uploaders.ImmichUploader, "_request", return_value={"name": "Clipplex"}
        ):
            with self.assertRaises(uploaders.UploadError):
                uploaders.ImmichUploader().get_api_key()

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

    def test_hierarchical_automatic_tags_and_description_are_applied(self):
        setting_values = {
            "immich_url": "http://immich:2283",
            "immich_api_key": "secret",
            "immich_auto_tag_library": "true",
            "immich_auto_tag_title": "true",
            "immich_auto_tag_episode": "true",
        }
        with self.environment(), patch("app.uploaders._setting", side_effect=lambda key: setting_values.get(key, "")), \
             patch.object(uploaders.ImmichUploader, "_upload_asset", return_value={"id": "asset-1"}), \
             patch.object(uploaders.ImmichUploader, "_update_description") as description, \
             patch.object(uploaders.ImmichUploader, "_hierarchy_tag_ids", return_value=["episode-tag-id"]) as hierarchy, \
             patch.object(uploaders.ImmichUploader, "_assign_tags") as tags:
            uploaders.ImmichUploader().upload(
                Path("clip.mp4"), [], [], [], "", "My Clip", {
                    "media_library": "Anime", "media_type": "episode", "show": "My Love Story",
                    "season_number": "1", "episode_number": "6",
                },
            )

        description.assert_called_once_with("asset-1", "My Clip")
        hierarchy.assert_called_once()
        tags.assert_called_once_with("asset-1", ["episode-tag-id"], [])

    def test_description_update_uses_single_asset_upsert_and_verifies_value(self):
        with self.environment(), patch.object(uploaders.ImmichUploader, "_request") as request:
            request.side_effect = [
                {"id": "asset-1"},
                {"id": "asset-1", "exifInfo": {"description": "My Clip"}},
            ]
            uploaders.ImmichUploader()._update_description("asset-1", "My Clip")

        self.assertEqual(request.call_args_list, [
            call("PUT", "/assets/asset-1", json={"description": "My Clip"}),
            call("GET", "/assets/asset-1"),
        ])

    def test_description_update_fails_when_immich_does_not_retain_value(self):
        with self.environment(), patch.object(uploaders.ImmichUploader, "_request") as request:
            request.side_effect = [
                {"id": "asset-1"},
                {"id": "asset-1", "exifInfo": {"description": ""}},
            ]
            with self.assertRaises(uploaders.UploadError) as raised:
                uploaders.ImmichUploader()._update_description("asset-1", "My Clip")

        self.assertIn("did not retain", raised.exception.message)

    def test_asset_exists_distinguishes_missing_assets_from_connection_failures(self):
        with self.environment(), patch("app.uploaders.requests.request") as request:
            request.return_value = ResponseStub({"message": "Asset not found"}, 400)
            self.assertFalse(uploaders.ImmichUploader().asset_exists("asset-1"))

            request.return_value = ResponseStub({"message": "Permission denied"}, 403)
            with self.assertRaises(uploaders.UploadError):
                uploaders.ImmichUploader().asset_exists("asset-1")

    def test_ambiguous_missing_asset_is_confirmed_with_current_key_permissions(self):
        with self.environment(), patch("app.uploaders.requests.request") as request:
            request.side_effect = [
                ResponseStub({"message": "Not found or no asset.read access"}, 400),
                ResponseStub({
                    "id": "key-id",
                    "name": "Clipplex",
                    "permissions": ["asset.read", "asset.upload"],
                }),
            ]

            self.assertFalse(uploaders.ImmichUploader().asset_exists("asset-1"))

        self.assertEqual(
            [item.args[:2] for item in request.call_args_list],
            [
                ("GET", "http://immich:2283/api/assets/asset-1"),
                ("GET", "http://immich:2283/api/api-keys/me"),
            ],
        )

    def test_ambiguous_missing_asset_is_not_assumed_deleted_without_read_permission(self):
        with self.environment(), patch("app.uploaders.requests.request") as request:
            request.side_effect = [
                ResponseStub({"message": "Not found or no asset.read access"}, 400),
                ResponseStub({
                    "id": "key-id",
                    "name": "Clipplex",
                    "permissions": ["asset.upload"],
                }),
            ]

            with self.assertRaises(uploaders.UploadError) as raised:
                uploaders.ImmichUploader().asset_exists("asset-1")

        self.assertIn("Not found or no asset.read access", raised.exception.message)

    def test_hierarchy_reuses_existing_parent_and_assigns_only_the_leaf(self):
        setting_values = {
            "immich_url": "http://immich:2283", "immich_api_key": "secret",
            "immich_auto_tag_library": "true", "immich_auto_tag_title": "true",
            "immich_auto_tag_episode": "true",
        }
        with patch("app.uploaders._setting", side_effect=lambda key: setting_values.get(key, "")), \
             patch.object(uploaders.ImmichUploader, "get_tags", return_value=[
                 {"id": "library-id", "name": "Anime", "parent_id": None},
             ]), patch.object(uploaders.ImmichUploader, "_request") as request:
            request.side_effect = [
                {"id": "show-id", "value": "Anime/My Love Story"},
                {"id": "episode-id", "value": "Anime/My Love Story/S01E06"},
            ]
            leaf_ids = uploaders.ImmichUploader()._hierarchy_tag_ids({
                "media_library": "Anime", "media_type": "episode", "show": "My Love Story",
                "season_number": "1", "episode_number": "6",
            })

        self.assertEqual(leaf_ids, ["episode-id"])
        self.assertEqual(request.call_args_list, [
            call("POST", "/tags", json={"name": "My Love Story", "parentId": "library-id"}),
            call("POST", "/tags", json={"name": "S01E06", "parentId": "show-id"}),
        ])

    def test_description_failure_retains_asset_id_as_partial_success(self):
        with self.environment(), patch.object(uploaders.ImmichUploader, "_upload_asset", return_value={"id": "asset-1"}), \
             patch.object(uploaders.ImmichUploader, "_update_description", side_effect=uploaders.UploadError("Update denied")), \
             patch.object(uploaders.ImmichUploader, "_assign_tags"):
            result, status = uploaders.ImmichUploader().upload(
                Path("clip.mp4"), [], [], [], "", "My Clip",
            )

        self.assertEqual(status, 207)
        self.assertEqual(result["asset_id"], "asset-1")
        self.assertEqual(result["failures"], [{"step": "description", "message": "Update denied"}])

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
