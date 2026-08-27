import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import threading


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent / "static" / "media" / ".clipplex" / "clipplex.sqlite3"
DATABASE_PATH_ENV = "CLIPPLEX_DATABASE_PATH"
SCHEMA_VERSION = 1
_INITIALIZE_LOCK = threading.RLock()
_INITIALIZED_PATHS = set()


def database_path() -> Path:
    configured = (os.environ.get(DATABASE_PATH_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DATABASE_PATH.resolve()


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def open_connection():
    active = connect()
    try:
        yield active
    finally:
        active.close()


@contextmanager
def transaction(immediate=False):
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> Path:
    with _INITIALIZE_LOCK:
        path = database_path()
        if path in _INITIALIZED_PATHS and path.is_file():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        with open_connection() as active:
            active.execute("PRAGMA journal_mode = WAL")
            version = active.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Clipplex database schema {version} is newer than supported schema {SCHEMA_VERSION}."
                )
            if version < 1:
                _migrate_to_v1(active)
                active.execute("PRAGMA user_version = 1")
            active.commit()
        _INITIALIZED_PATHS.add(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY,
            file_path TEXT NOT NULL UNIQUE,
            file_size INTEGER NOT NULL DEFAULT 0,
            file_mtime_ns INTEGER NOT NULL DEFAULT 0,
            revision TEXT NOT NULL DEFAULT '',
            analysis_status TEXT NOT NULL DEFAULT 'pending',
            analysis_error TEXT NOT NULL DEFAULT '',
            analyzed_at TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            media_library TEXT NOT NULL DEFAULT 'Uncategorized',
            media_type TEXT NOT NULL DEFAULT 'movie' CHECK (media_type IN ('movie', 'episode')),
            title TEXT NOT NULL DEFAULT '',
            show TEXT NOT NULL DEFAULT '',
            season_number TEXT NOT NULL DEFAULT '',
            episode_number TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            original_start_time TEXT NOT NULL DEFAULT '00:00:00.000',
            original_end_time TEXT NOT NULL DEFAULT '',
            source_key TEXT NOT NULL,
            clip_number INTEGER NOT NULL DEFAULT 1 CHECK (clip_number > 0),
            clip_title TEXT NOT NULL DEFAULT '',
            clip_title_custom INTEGER NOT NULL DEFAULT 0 CHECK (clip_title_custom IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            legacy_import_pending INTEGER NOT NULL DEFAULT 0 CHECK (legacy_import_pending IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS clip_sources (
            clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
            version INTEGER NOT NULL DEFAULT 1,
            rating_key TEXT NOT NULL DEFAULT '',
            media_key TEXT NOT NULL DEFAULT '',
            part_id TEXT NOT NULL DEFAULT '',
            media_path TEXT NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            file_size INTEGER NOT NULL DEFAULT 0,
            file_mtime_ns INTEGER NOT NULL DEFAULT 0,
            video_stream_index INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS clip_source_tracks (
            clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
            track_kind TEXT NOT NULL CHECK (track_kind IN ('audio', 'subtitle')),
            track_id TEXT NOT NULL DEFAULT '',
            stream_index INTEGER,
            track_type TEXT NOT NULL DEFAULT '',
            codec TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            plex_key TEXT NOT NULL DEFAULT '',
            selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
            available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
            unavailable_reason TEXT NOT NULL DEFAULT '',
            subtitle_index INTEGER,
            probe_codec TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (clip_id, track_kind)
        );

        CREATE INDEX IF NOT EXISTS clips_created_at_idx ON clips(created_at);
        CREATE INDEX IF NOT EXISTS clips_title_idx ON clips(clip_title COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS clips_duration_idx ON clips(duration_ms);
        CREATE INDEX IF NOT EXISTS clips_library_idx ON clips(media_library COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS clips_media_type_idx ON clips(media_type);
        CREATE UNIQUE INDEX IF NOT EXISTS clips_source_number_idx ON clips(source_key, clip_number);
        """
    )
