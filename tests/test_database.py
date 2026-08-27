import os
from pathlib import Path
import shutil
from unittest.mock import patch
import unittest
import uuid

from app import database, settings
from app.media_files import MEDIA_DIRECTORY


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.root = MEDIA_DIRECTORY / f"database-tests-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path_patch = patch("app.database.DEFAULT_DATABASE_PATH", self.root / "clipplex.sqlite3")
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_schema_is_versioned_indexed_and_idempotent(self):
        first = database.initialize_database()
        second = database.initialize_database()

        self.assertEqual(first, second)
        with database.open_connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], database.SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            clip_columns = {row["name"] for row in connection.execute("PRAGMA table_info(clips)")}
        self.assertTrue({"settings", "clips", "clip_sources", "clip_source_tracks"}.issubset(tables))
        self.assertTrue({"clips_created_at_idx", "clips_title_idx", "clips_duration_idx", "clips_source_number_idx"}.issubset(indexes))
        self.assertNotIn("legacy_import_pending", clip_columns)

    def test_schema_v1_obsolete_flag_is_removed_on_upgrade(self):
        with database.open_connection() as connection:
            database._migrate_to_v1(connection)
            connection.execute(
                "ALTER TABLE clips ADD COLUMN legacy_import_pending INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        database.initialize_database()

        with database.open_connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            clip_columns = {row["name"] for row in connection.execute("PRAGMA table_info(clips)")}
        self.assertEqual(version, 2)
        self.assertNotIn("legacy_import_pending", clip_columns)

    def test_environment_values_override_then_survive_removal(self):
        with patch.dict(os.environ, {"PLEX_URL": " http://plex:32400/ ", "PLEX_TOKEN": " first "}, clear=True):
            settings.initialize_settings()
            self.assertEqual(settings.get("plex_url"), "http://plex:32400/")
            self.assertEqual(settings.get("plex_token"), "first")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("plex_url"), "http://plex:32400/")
            self.assertEqual(settings.get("plex_token"), "first")

        with patch.dict(os.environ, {"PLEX_TOKEN": "second"}, clear=True):
            settings.initialize_settings()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("plex_token"), "second")

    def test_blank_values_do_not_erase_settings_and_app_secret_is_stable(self):
        with patch.dict(os.environ, {"IMMICH_API_KEY": "secret"}, clear=True):
            settings.initialize_settings()
            first_app_secret = settings.get("flask_secret_key")
        with patch.dict(os.environ, {"IMMICH_API_KEY": "   "}, clear=True):
            settings.initialize_settings()
            self.assertEqual(settings.get("immich_api_key"), "secret")
            self.assertEqual(settings.get("flask_secret_key"), first_app_secret)
        with database.open_connection() as connection:
            secret_keys = {row["key"] for row in connection.execute("SELECT key FROM settings WHERE is_secret = 1")}
        self.assertTrue({"immich_api_key", "flask_secret_key"}.issubset(secret_keys))

    def test_required_plex_validation_uses_stored_values(self):
        with patch.dict(os.environ, {}, clear=True):
            settings.initialize_settings()
            with self.assertRaisesRegex(RuntimeError, "PLEX_URL, PLEX_TOKEN"):
                settings.require_plex_settings()
        with patch.dict(os.environ, {"PLEX_URL": "http://plex", "PLEX_TOKEN": "token"}, clear=True):
            settings.initialize_settings()
        with patch.dict(os.environ, {}, clear=True):
            settings.require_plex_settings()


if __name__ == "__main__":
    unittest.main()
