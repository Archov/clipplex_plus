import os
import secrets

from app import database


SETTING_DEFINITIONS = {
    "plex_url": {"environment": "PLEX_URL", "secret": False},
    "plex_token": {"environment": "PLEX_TOKEN", "secret": True},
    "streamable_login": {"environment": "STREAMABLE_LOGIN", "secret": False},
    "streamable_password": {"environment": "STREAMABLE_PASSWORD", "secret": True},
    "immich_url": {"environment": "IMMICH_URL", "secret": False},
    "immich_api_key": {"environment": "IMMICH_API_KEY", "secret": True},
    "immich_default_tag": {"environment": "IMMICH_DEFAULT_TAG", "secret": False},
    "ffmpeg_preset": {"environment": "FFMPEG_PRESET", "secret": False},
    "flask_secret_key": {"environment": "CLIPPLEX_SECRET_KEY", "secret": True},
}


def initialize_settings() -> None:
    database.initialize_database()
    with database.transaction(immediate=True) as connection:
        for key, definition in SETTING_DEFINITIONS.items():
            value = (os.environ.get(definition["environment"]) or "").strip()
            if value:
                _upsert(connection, key, value, definition["secret"])
        if connection.execute("SELECT 1 FROM settings WHERE key = 'ffmpeg_preset'").fetchone() is None:
            _upsert(connection, "ffmpeg_preset", "veryfast", False)
        if connection.execute("SELECT 1 FROM settings WHERE key = 'flask_secret_key'").fetchone() is None:
            _upsert(connection, "flask_secret_key", secrets.token_urlsafe(48), True)


def _upsert(connection, key: str, value: str, is_secret: bool) -> None:
    connection.execute(
        """
        INSERT INTO settings (key, value, is_secret, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            is_secret = excluded.is_secret,
            updated_at = CURRENT_TIMESTAMP
        """,
        (key, value, int(is_secret)),
    )


def get(key: str, default: str = "") -> str:
    if key not in SETTING_DEFINITIONS:
        raise KeyError(f"Unknown Clipplex setting: {key}")
    database.initialize_database()
    definition = SETTING_DEFINITIONS[key]
    environment_value = (os.environ.get(definition["environment"]) or "").strip()
    if environment_value:
        with database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row is None or row["value"] != environment_value:
                _upsert(connection, key, environment_value, definition["secret"])
        return environment_value
    with database.open_connection() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def require_plex_settings() -> None:
    missing = [label for key, label in (("plex_url", "PLEX_URL"), ("plex_token", "PLEX_TOKEN")) if not get(key)]
    if missing:
        raise RuntimeError(
            "Missing required Plex configuration: " + ", ".join(missing)
            + ". Supply it once through the environment so Clipplex can store it in SQLite."
        )
