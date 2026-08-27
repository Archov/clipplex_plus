import os
import secrets
from urllib.parse import urlsplit

from app import database


class SettingsError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


SETTING_DEFINITIONS = {
    "plex_url": {
        "environment": "PLEX_URL",
        "secret": False,
        "section": "plex",
        "label": "Plex URL",
        "help": "The base URL of your Plex Media Server.",
        "kind": "url",
    },
    "plex_token": {
        "environment": "PLEX_TOKEN",
        "secret": True,
        "section": "plex",
        "label": "Plex token",
        "help": "Authentication token used to access Plex.",
        "kind": "password",
        "help_url": "https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/",
        "help_link_label": "How do I find this?",
    },
    "streamable_login": {
        "environment": "STREAMABLE_LOGIN",
        "secret": False,
        "section": "streamable",
        "label": "Streamable email",
        "help": "Account email used for Streamable uploads.",
        "kind": "email",
    },
    "streamable_password": {
        "environment": "STREAMABLE_PASSWORD",
        "secret": True,
        "section": "streamable",
        "label": "Streamable password",
        "help": "Password used for Streamable uploads.",
        "kind": "password",
    },
    "immich_url": {
        "environment": "IMMICH_URL",
        "secret": False,
        "section": "immich",
        "label": "Immich URL",
        "help": "The base URL of your Immich server.",
        "kind": "url",
    },
    "immich_api_key": {
        "environment": "IMMICH_API_KEY",
        "secret": True,
        "section": "immich",
        "label": "Immich API key",
        "help": "API key used for Immich uploads.",
        "kind": "password",
        "permissions": [
            "asset.upload",
            "asset.update",
            "asset.delete (only if 'Manage Immich clips after upload' is enabled)",
            "tag.read",
            "tag.create",
            "tag.asset",
            "album.read",
            "album.create",
            "albumAsset.create",
        ],
    },
    "immich_default_tag": {
        "environment": "IMMICH_DEFAULT_TAG",
        "secret": False,
        "section": "immich",
        "label": "Default Immich tag",
        "help": "Optional tag added to every Immich upload.",
        "kind": "text",
    },
    "immich_auto_upload": {
        "environment": None,
        "secret": False,
        "section": "immich",
        "label": "Auto upload new clips",
        "help": "Upload newly created and replacement clips to Immich.",
        "kind": "checkbox",
    },
    "immich_manage_assets": {
        "environment": None,
        "secret": False,
        "section": "immich",
        "label": "Manage Immich clips after upload",
        "help": "Allow Clipplex to delete linked Immich assets during replacement or deletion.",
        "kind": "checkbox",
    },
    "immich_auto_tag_library": {
        "environment": None,
        "secret": False,
        "section": "immich",
        "group": "Auto-Tag:",
        "label": "Media Library Name",
        "help": "Use the Plex library as an automatic tag.",
        "kind": "checkbox",
    },
    "immich_auto_tag_title": {
        "environment": None,
        "secret": False,
        "section": "immich",
        "group": "Auto-Tag:",
        "label": "Show/Movie Name",
        "help": "Use the show or movie name as an automatic tag.",
        "kind": "checkbox",
    },
    "immich_auto_tag_episode": {
        "environment": None,
        "secret": False,
        "section": "immich",
        "group": "Auto-Tag:",
        "label": "Episode Title (S##E##)",
        "help": "Use S##E## as an automatic tag for episodes.",
        "kind": "checkbox",
    },
    "ffmpeg_preset": {
        "environment": "FFMPEG_PRESET",
        "secret": False,
        "section": "encoding",
        "label": "FFmpeg preset",
        "help": "Encoding speed/quality preset used for new clips.",
        "kind": "select",
    },
    "flask_secret_key": {"environment": "CLIPPLEX_SECRET_KEY", "secret": True},
}

UI_SETTING_KEYS = tuple(
    key for key, definition in SETTING_DEFINITIONS.items() if "section" in definition
)
SECTION_LABELS = {
    "plex": "Plex",
    "streamable": "Streamable",
    "immich": "Immich",
    "encoding": "Encoding",
}
MAX_SETTING_LENGTH = 4096


def initialize_settings() -> None:
    database.initialize_database()
    with database.transaction(immediate=True) as connection:
        for key, definition in SETTING_DEFINITIONS.items():
            value = (
                (os.environ.get(definition["environment"]) or "").strip()
                if definition.get("environment")
                else ""
            )
            if value:
                _upsert(connection, key, value, definition["secret"])
        if (
            connection.execute(
                "SELECT 1 FROM settings WHERE key = 'ffmpeg_preset'"
            ).fetchone()
            is None
        ):
            _upsert(connection, "ffmpeg_preset", "veryfast", False)
        if (
            connection.execute(
                "SELECT 1 FROM settings WHERE key = 'flask_secret_key'"
            ).fetchone()
            is None
        ):
            _upsert(connection, "flask_secret_key", secrets.token_urlsafe(48), True)
        for key in (
            "immich_auto_upload",
            "immich_manage_assets",
            "immich_auto_tag_library",
            "immich_auto_tag_title",
            "immich_auto_tag_episode",
        ):
            if (
                connection.execute(
                    "SELECT 1 FROM settings WHERE key = ?", (key,)
                ).fetchone()
                is None
            ):
                _upsert(connection, key, "false", False)


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
    environment_value = (
        (os.environ.get(definition["environment"]) or "").strip()
        if definition.get("environment")
        else ""
    )
    if environment_value:
        with database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            if row is None or row["value"] != environment_value:
                _upsert(connection, key, environment_value, definition["secret"])
        return environment_value
    with database.open_connection() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row is not None else default


def _environment_value(key: str) -> str:
    environment = SETTING_DEFINITIONS[key].get("environment")
    return (os.environ.get(environment) or "").strip() if environment else ""


def _validate_url(key: str, value: str) -> None:
    if not value:
        return
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SettingsError(
            f"{SETTING_DEFINITIONS[key]['label']} must be a complete HTTP(S) URL."
        )


def _validate_value(key: str, value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) > MAX_SETTING_LENGTH:
        raise SettingsError(
            f"{SETTING_DEFINITIONS[key]['label']} must contain at most {MAX_SETTING_LENGTH} characters."
        )
    if SETTING_DEFINITIONS[key].get("kind") == "url":
        _validate_url(key, cleaned)
    if key == "ffmpeg_preset":
        from clipplexAPI import X264_PRESETS

        if cleaned not in X264_PRESETS:
            raise SettingsError("Select a supported FFmpeg preset.")
    if SETTING_DEFINITIONS[key].get("kind") == "checkbox" and cleaned not in {
        "true",
        "false",
    }:
        raise SettingsError(
            f"{SETTING_DEFINITIONS[key]['label']} must be enabled or disabled."
        )
    return cleaned


def _ui_field(key: str) -> dict:
    definition = SETTING_DEFINITIONS[key]
    environment_value = _environment_value(key)
    field = {
        "key": key,
        "section": definition["section"],
        "label": definition["label"],
        "help": definition["help"],
        "kind": definition["kind"],
        "secret": definition["secret"],
        "environment_managed": bool(environment_value),
        "environment": definition["environment"],
    }
    if definition["secret"]:
        field["configured"] = bool(get(key))
    else:
        field["value"] = get(key)
    if key == "ffmpeg_preset":
        from clipplexAPI import X264_PRESETS

        field["options"] = sorted(X264_PRESETS)
    if "permissions" in definition:
        field["permissions"] = definition["permissions"]
    if "help_url" in definition:
        field["help_url"] = definition["help_url"]
        field["help_link_label"] = definition["help_link_label"]
    if "group" in definition:
        field["group"] = definition["group"]
    return field


def ui_settings() -> dict:
    """Return display-safe persisted-settings data for the management UI."""
    return {
        "sections": [
            {"id": key, "label": label} for key, label in SECTION_LABELS.items()
        ],
        "fields": [_ui_field(key) for key in UI_SETTING_KEYS],
    }


def update_ui_settings(values, clear_keys=None) -> dict:
    """Validate and atomically persist settings supplied through the management UI."""
    if not isinstance(values, dict):
        raise SettingsError("Settings values must be an object.")
    if clear_keys is None:
        clear_keys = []
    if not isinstance(clear_keys, list) or not all(
        isinstance(key, str) for key in clear_keys
    ):
        raise SettingsError("Settings to clear must be a list of field names.")
    supplied = set(values) | set(clear_keys)
    unknown = supplied - set(UI_SETTING_KEYS)
    if unknown:
        raise SettingsError("Unknown setting: " + sorted(unknown)[0])
    if set(values) & set(clear_keys):
        raise SettingsError(
            "A setting cannot be updated and cleared in the same request."
        )
    for key in supplied:
        if _environment_value(key):
            raise SettingsError(
                f"{SETTING_DEFINITIONS[key]['label']} is managed by {SETTING_DEFINITIONS[key]['environment']}. Remove it from the environment before editing this setting.",
                409,
            )
    cleaned_values = {}
    for key, value in values.items():
        if not isinstance(value, str):
            raise SettingsError(f"{SETTING_DEFINITIONS[key]['label']} must be text.")
        cleaned_values[key] = _validate_value(key, value)

    with database.transaction(immediate=True) as connection:
        for key, value in cleaned_values.items():
            _upsert(connection, key, value, SETTING_DEFINITIONS[key]["secret"])
        for key in clear_keys:
            connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        # Plex remains required; require both values after applying this complete transaction.
        effective = {
            key: (
                connection.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                or {"value": ""}
            )["value"]
            for key in ("plex_url", "plex_token")
        }
        if not all(effective.values()):
            raise SettingsError("Plex URL and Plex token must both remain configured.")
    return ui_settings()


def test_service(service: str) -> dict:
    """Exercise saved service credentials without returning sensitive data."""
    if service == "plex":
        if not get("plex_url") or not get("plex_token"):
            raise SettingsError(
                "Plex URL and Plex token must be configured before testing."
            )
        try:
            from clipplexAPI import PlexSessions

            PlexSessions().request_xml("/")
        except Exception as error:
            raise SettingsError(
                "Could not connect to Plex with the saved URL and token.", 502
            ) from error
        return {"service": service, "ok": True, "message": "Connected to Plex."}
    if service == "streamable":
        login, password = get("streamable_login"), get("streamable_password")
        if not login or not password:
            raise SettingsError(
                "Streamable email and password must be configured before testing."
            )
        try:
            import requests

            response = requests.get(
                "https://api.streamable.com/users/me",
                auth=(login, password),
                timeout=(5, 15),
            )
            response.raise_for_status()
        except Exception as error:
            raise SettingsError(
                "Could not connect to Streamable with the saved credentials.", 502
            ) from error
        return {"service": service, "ok": True, "message": "Connected to Streamable."}
    if service == "immich":
        if not get("immich_url") or not get("immich_api_key"):
            raise SettingsError(
                "Immich URL and API key must be configured before testing."
            )
        try:
            from app.uploaders import ImmichUploader

            ImmichUploader().get_tags()
        except Exception as error:
            raise SettingsError(
                "Could not connect to Immich with the saved URL and API key.", 502
            ) from error
        return {"service": service, "ok": True, "message": "Connected to Immich."}
    raise SettingsError("Select a supported service to test.")


def require_plex_settings() -> None:
    missing = [
        label
        for key, label in (("plex_url", "PLEX_URL"), ("plex_token", "PLEX_TOKEN"))
        if not get(key)
    ]
    if missing:
        raise RuntimeError(
            "Missing required Plex configuration: "
            + ", ".join(missing)
            + ". Supply it once through the environment so Clipplex can store it in SQLite."
        )
