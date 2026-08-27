import os
from pathlib import Path
import shutil
from unittest.mock import patch
import unittest
import uuid

from app import app, database, settings
from app.media_files import MEDIA_DIRECTORY


class SettingsUiTests(unittest.TestCase):
    def setUp(self):
        self.root = MEDIA_DIRECTORY / f"settings-ui-tests-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path_patch = patch("app.database.database_path", return_value=(self.root / "clipplex.sqlite3").resolve())
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.environment = patch.dict(os.environ, {
            "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "plex-token",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        settings.initialize_settings()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_settings_page_and_api_redact_secrets(self):
        response = self.client.get("/settings.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings", response.data)
        self.assertIn(b"/static/js/settings.js", response.data)

        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        fields = {field["key"]: field for field in payload["fields"]}
        self.assertEqual(fields["plex_url"]["value"], "http://plex:32400")
        self.assertTrue(fields["plex_token"]["configured"])
        self.assertNotIn("value", fields["plex_token"])
        self.assertEqual(
            fields["plex_token"]["help_url"],
            "https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/",
        )
        self.assertEqual(fields["immich_api_key"]["permissions"], [
            "asset.upload", "tag.read", "tag.create", "tag.asset",
            "album.read", "album.create", "albumAsset.create",
        ])
        self.assertNotIn('"value":"plex-token"', response.get_data(as_text=True))
        self.assertNotIn("flask_secret_key", fields)

    def test_patch_persists_secret_without_disclosing_it(self):
        response = self.client.patch("/api/settings", json={
            "values": {"immich_url": "https://immich.example", "immich_api_key": "immich-secret"},
            "clear": [],
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("immich-secret", response.get_data(as_text=True))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("immich_url"), "https://immich.example")
            self.assertEqual(settings.get("immich_api_key"), "immich-secret")

    def test_patch_rejects_invalid_preset_without_partial_write(self):
        response = self.client.patch("/api/settings", json={
            "values": {"immich_default_tag": "new-tag", "ffmpeg_preset": "not-a-preset"},
            "clear": [],
        })
        self.assertEqual(response.status_code, 400)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("immich_default_tag"), "")
            self.assertEqual(settings.get("ffmpeg_preset"), "veryfast")

    def test_environment_managed_settings_cannot_be_updated(self):
        response = self.client.patch("/api/settings", json={
            "values": {"plex_url": "http://other-plex:32400"}, "clear": [],
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("managed by PLEX_URL", response.get_json()["message"])

    def test_explicit_secret_clear_and_required_plex_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.patch("/api/settings", json={"values": {}, "clear": ["plex_token"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(settings.get("plex_token"), "plex-token")

        response = self.client.patch("/api/settings", json={
            "values": {"streamable_login": "clipper@example.com", "streamable_password": "pass"}, "clear": [],
        })
        self.assertEqual(response.status_code, 200)
        response = self.client.patch("/api/settings", json={"values": {}, "clear": ["streamable_password"]})
        self.assertEqual(response.status_code, 200)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("streamable_password"), "")

    @patch("clipplexAPI.PlexSessions.request_xml")
    def test_plex_connection_test_reports_success(self, request_xml):
        response = self.client.post("/api/settings/tests", json={"service": "plex"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "service": "plex", "ok": True, "message": "Connected to Plex.",
        })
        request_xml.assert_called_once_with("/")

    @patch("clipplexAPI.PlexSessions.request_xml", side_effect=Exception("token=plex-token"))
    def test_connection_test_hides_upstream_error_details(self, request_xml):
        response = self.client.post("/api/settings/tests", json={"service": "plex"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["message"], "Could not connect to Plex with the saved URL and token.")
        self.assertNotIn("plex-token", response.get_data(as_text=True))

    @patch("requests.get")
    def test_streamable_connection_test_uses_saved_credentials(self, request_get):
        request_get.return_value.raise_for_status.return_value = None
        with patch.dict(os.environ, {}, clear=True):
            settings.update_ui_settings({
                "streamable_login": "clipper@example.com", "streamable_password": "streamable-secret",
            })
            response = self.client.post("/api/settings/tests", json={"service": "streamable"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Connected to Streamable.")
        self.assertEqual(request_get.call_args.kwargs["auth"], ("clipper@example.com", "streamable-secret"))

    @patch("app.uploaders.ImmichUploader.get_tags")
    def test_immich_connection_test_uses_saved_configuration(self, get_tags):
        with patch.dict(os.environ, {}, clear=True):
            settings.update_ui_settings({
                "immich_url": "https://immich.example", "immich_api_key": "immich-secret",
            })
            response = self.client.post("/api/settings/tests", json={"service": "immich"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Connected to Immich.")
        get_tags.assert_called_once()

    def test_unknown_test_service_is_rejected(self):
        response = self.client.post("/api/settings/tests", json={"service": "unknown"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["ok"], False)


if __name__ == "__main__":
    unittest.main()
