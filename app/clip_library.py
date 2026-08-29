import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import ffmpeg

from app import database
from app.media_files import (
    MediaFileError, THUMBNAIL_DIRECTORY, VIDEO_DIRECTORY, lock_for_clip,
    public_media_path, resolve_generated_clip, thumbnail_path_for_clip,
)


LOGGER = logging.getLogger(__name__)
TEXT_FIELDS = ("media_library", "title", "show", "season_number", "episode_number", "year")
TIMELINE_FIELDS = ("original_start_time", "original_end_time")
VALID_MEDIA_TYPES = {"movie", "episode"}
MAX_TEXT_LENGTH = 255
SORT_QUERIES = {
    "newest": "SELECT * FROM clips ORDER BY created_at DESC, file_path ASC",
    "oldest": "SELECT * FROM clips ORDER BY created_at ASC, file_path ASC",
    "title_asc": "SELECT * FROM clips ORDER BY clip_title COLLATE NOCASE ASC, created_at DESC",
    "title_desc": "SELECT * FROM clips ORDER BY clip_title COLLATE NOCASE DESC, created_at DESC",
    "duration_asc": "SELECT * FROM clips ORDER BY duration_ms ASC, created_at DESC",
    "duration_desc": "SELECT * FROM clips ORDER BY duration_ms DESC, created_at DESC",
}
SORT_LIMIT_QUERIES = {
    "newest": "SELECT * FROM clips ORDER BY created_at DESC, file_path ASC LIMIT ?",
    "oldest": "SELECT * FROM clips ORDER BY created_at ASC, file_path ASC LIMIT ?",
    "title_asc": "SELECT * FROM clips ORDER BY clip_title COLLATE NOCASE ASC, created_at DESC LIMIT ?",
    "title_desc": "SELECT * FROM clips ORDER BY clip_title COLLATE NOCASE DESC, created_at DESC LIMIT ?",
    "duration_asc": "SELECT * FROM clips ORDER BY duration_ms ASC, created_at DESC LIMIT ?",
    "duration_desc": "SELECT * FROM clips ORDER BY duration_ms DESC, created_at DESC LIMIT ?",
}
SORT_ORDERS = frozenset(SORT_QUERIES)
_CLIP_UPDATE_FIELDS = (
    "file_size", "file_mtime_ns", "revision", "analysis_status", "analysis_error", "duration_ms",
    "media_library", "media_type", "title", "show", "season_number", "episode_number", "year",
    "username", "original_start_time", "original_end_time", "source_key", "clip_number", "clip_title",
    "clip_title_custom", "immich_asset_id", "created_at",
)
_CLIP_UPDATE_SQL = """
    UPDATE clips SET
        file_size = COALESCE(:file_size, file_size),
        file_mtime_ns = COALESCE(:file_mtime_ns, file_mtime_ns),
        revision = COALESCE(:revision, revision),
        analysis_status = COALESCE(:analysis_status, analysis_status),
        analysis_error = COALESCE(:analysis_error, analysis_error),
        duration_ms = COALESCE(:duration_ms, duration_ms),
        media_library = COALESCE(:media_library, media_library),
        media_type = COALESCE(:media_type, media_type),
        title = COALESCE(:title, title),
        show = COALESCE(:show, show),
        season_number = COALESCE(:season_number, season_number),
        episode_number = COALESCE(:episode_number, episode_number),
        year = COALESCE(:year, year),
        username = COALESCE(:username, username),
        original_start_time = COALESCE(:original_start_time, original_start_time),
        original_end_time = COALESCE(:original_end_time, original_end_time),
        source_key = COALESCE(:source_key, source_key),
        clip_number = COALESCE(:clip_number, clip_number),
        clip_title = COALESCE(:clip_title, clip_title),
        clip_title_custom = COALESCE(:clip_title_custom, clip_title_custom),
        immich_asset_id = COALESCE(:immich_asset_id, immich_asset_id),
        created_at = COALESCE(:created_at, created_at),
        analyzed_at = CASE WHEN :mark_analyzed THEN CURRENT_TIMESTAMP ELSE analyzed_at END,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :clip_id
"""


def _utc_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _four_digit_year(value) -> str:
    match = re.search(r"(?:^|\D)(\d{4})(?:\D|$)", _clean(value))
    return match.group(1) if match else ""


def _episode_code(season, episode) -> str:
    season_value, episode_value = _clean(season), _clean(episode)
    if not season_value or not episode_value:
        return ""
    if season_value.isdigit():
        season_value = season_value.zfill(2)
    if episode_value.isdigit():
        episode_value = episode_value.zfill(2)
    return f"S{season_value}E{episode_value}"


def _positive_integer(value, default=1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _nonnegative_integer(value, default=0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _optional_integer(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_key(details: dict) -> str:
    media_type = _clean(details.get("media_type")).lower()
    library = (_clean(details.get("media_library")) or "Uncategorized").casefold()
    if media_type == "episode":
        season, episode = _clean(details.get("season_number")), _clean(details.get("episode_number"))
        season = str(int(season)) if season.isdigit() else season.casefold()
        episode = str(int(episode)) if episode.isdigit() else episode.casefold()
        identity = ["episode", library, _clean(details.get("show")).casefold(), season, episode]
    else:
        identity = ["movie", library, _clean(details.get("title")).casefold(), _four_digit_year(details.get("year"))]
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _base_clip_title(details: dict) -> str:
    if _clean(details.get("media_type")).lower() == "episode":
        parts = [_clean(details.get("show")) or "Unknown Series",
                 _episode_code(details.get("season_number"), details.get("episode_number")),
                 _clean(details.get("title")) or "Untitled episode"]
        return " - ".join(part for part in parts if part)
    title, year = _clean(details.get("title")) or "Untitled movie", _four_digit_year(details.get("year"))
    return f"{title} ({year})" if year else title


def _numbered_clip_title(base_title: str, clip_number: int) -> str:
    number = _positive_integer(clip_number)
    suffix = f" - {number}" if number > 1 else ""
    return f"{(_clean(base_title) or 'Clip')[:MAX_TEXT_LENGTH - len(suffix)].rstrip()}{suffix}"


def _timestamp_milliseconds(value: str) -> int:
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?", _clean(value))
    if not match:
        return 0
    fraction = (match.group(4) or "0").ljust(3, "0")
    return ((int(match.group(1)) * 60 + int(match.group(2))) * 60 + int(match.group(3))) * 1000 + int(fraction)


def _format_timestamp(milliseconds: int) -> str:
    value = max(0, int(milliseconds))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _resolve_clip_path(file_path) -> Path:
    candidate = Path(file_path)
    if candidate.is_absolute():
        return resolve_generated_clip(public_media_path(candidate))
    normalized = candidate.as_posix()
    return resolve_generated_clip(normalized[4:] if normalized.startswith("app/") else normalized)


def _probe(clip_path: Path) -> dict:
    probe = ffmpeg.probe(str(clip_path))
    return probe if isinstance(probe, dict) else {}


def _duration_from_probe(probe: dict) -> int:
    try:
        return max(0, round(float((probe.get("format") or {}).get("duration") or 0) * 1000))
    except (TypeError, ValueError):
        return 0


def _next_clip_number(connection, source_key: str, excluded_id=None) -> int:
    if excluded_id is None:
        row = connection.execute(
            "SELECT COALESCE(MAX(clip_number), 0) FROM clips WHERE source_key = ?",
            (source_key,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT COALESCE(MAX(clip_number), 0) FROM clips WHERE source_key = ? AND id != ?",
            (source_key, excluded_id),
        ).fetchone()
    return int(row[0]) + 1


def _number_is_available(connection, source_key: str, number: int, excluded_id=None) -> bool:
    if excluded_id is None:
        row = connection.execute(
            "SELECT 1 FROM clips WHERE source_key = ? AND clip_number = ?",
            (source_key, number),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM clips WHERE source_key = ? AND clip_number = ? AND id != ?",
            (source_key, number, excluded_id),
        ).fetchone()
    return row is None


def _update_clip(connection, clip_id: int, values: dict, analyzed=False) -> None:
    unknown_columns = values.keys() - set(_CLIP_UPDATE_FIELDS)
    if unknown_columns:
        raise ValueError(f"Unsupported clip columns: {', '.join(sorted(unknown_columns))}")
    parameters = {field: None for field in _CLIP_UPDATE_FIELDS}
    parameters.update(values)
    parameters.update({
        "clip_id": clip_id,
        "mark_analyzed": int(analyzed),
    })
    connection.execute(_CLIP_UPDATE_SQL, parameters)


def _save_source(connection, clip_id: int, source) -> None:
    connection.execute("DELETE FROM clip_source_tracks WHERE clip_id = ?", (clip_id,))
    connection.execute("DELETE FROM clip_sources WHERE clip_id = ?", (clip_id,))
    if not isinstance(source, dict) or not _clean(source.get("media_path")):
        return
    fingerprint = source.get("fingerprint") if isinstance(source.get("fingerprint"), dict) else {}
    connection.execute(
        "INSERT INTO clip_sources (clip_id, version, rating_key, media_key, part_id, media_path, duration_ms, file_size, file_mtime_ns, video_stream_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clip_id, _positive_integer(source.get("version")), _clean(source.get("rating_key")),
         _clean(source.get("media_key")), _clean(source.get("part_id")), _clean(source.get("media_path")),
         _nonnegative_integer(source.get("duration_ms")), _nonnegative_integer(fingerprint.get("size")),
         _nonnegative_integer(fingerprint.get("mtime_ns")), _nonnegative_integer(source.get("video_stream_index"))),
    )
    for kind, key in (("audio", "audio_track"), ("subtitle", "subtitle_track")):
        track = source.get(key)
        if not isinstance(track, dict):
            continue
        connection.execute(
            "INSERT INTO clip_source_tracks (clip_id, track_kind, track_id, stream_index, track_type, codec, language, title, plex_key, selected, available, unavailable_reason, subtitle_index, probe_codec) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (clip_id, kind, _clean(track.get("id")), _optional_integer(track.get("index")), _clean(track.get("track_type")),
             _clean(track.get("codec")), _clean(track.get("language")), _clean(track.get("title")),
             _clean(track.get("key")), int(bool(track.get("selected"))), int(track.get("available", True) is not False),
             _clean(track.get("unavailable_reason")), _optional_integer(track.get("subtitle_index")), _clean(track.get("probe_codec"))),
        )


def _source_for_clip(connection, clip_id: int):
    source = connection.execute("SELECT * FROM clip_sources WHERE clip_id = ?", (clip_id,)).fetchone()
    if source is None:
        return None
    result = {"version": source["version"], "rating_key": source["rating_key"], "media_key": source["media_key"],
              "part_id": source["part_id"], "media_path": source["media_path"], "duration_ms": source["duration_ms"],
              "fingerprint": {"size": source["file_size"], "mtime_ns": source["file_mtime_ns"]},
              "video_stream_index": source["video_stream_index"], "audio_track": None, "subtitle_track": None}
    for track in connection.execute("SELECT * FROM clip_source_tracks WHERE clip_id = ?", (clip_id,)):
        result[f"{track['track_kind']}_track"] = {
            "id": track["track_id"], "index": track["stream_index"], "track_type": track["track_type"],
            "codec": track["codec"], "language": track["language"], "title": track["title"], "key": track["plex_key"],
            "selected": bool(track["selected"]), "available": bool(track["available"]),
            "unavailable_reason": track["unavailable_reason"], "subtitle_index": track["subtitle_index"],
            "probe_codec": track["probe_codec"],
        }
    return result


def _metadata_from_row(connection, row) -> dict:
    result = {key: row[key] for key in TEXT_FIELDS + TIMELINE_FIELDS}
    result.update({"media_type": row["media_type"], "username": row["username"], "source_key": row["source_key"],
                   "clip_number": row["clip_number"], "clip_title": row["clip_title"],
                   "clip_title_custom": bool(row["clip_title_custom"]), "immich_asset_id": row["immich_asset_id"], "created_at": row["created_at"], "version": 1})
    source = _source_for_clip(connection, row["id"])
    if source is not None:
        result["source"] = source
    return result


def _insert_new_clip(connection, clip_path: Path, probe, analysis_error) -> int:
    file_stat = clip_path.stat()
    tags = (probe.get("format") or {}).get("tags") or {}

    def value(key, *fallback_keys):
        return next((_clean(tags.get(candidate)) for candidate in (key,) + fallback_keys if _clean(tags.get(candidate))), "")

    title, show = value("title"), value("show")
    season, episode = value("season_number"), value("episode_number", "episode_id")
    inferred_type = "episode" if show or season or episode else "movie"
    media_type = value("media_type").lower() or inferred_type
    media_type = media_type if media_type in VALID_MEDIA_TYPES else inferred_type
    details = {"media_library": value("media_library", "album") or "Uncategorized", "media_type": media_type,
               "title": title, "show": show if media_type == "episode" else "",
               "season_number": season if media_type == "episode" else "",
               "episode_number": episode if media_type == "episode" else "",
               "year": _four_digit_year(value("year", "date")) if media_type == "movie" else ""}
    source_key = _source_key(details)
    number = _next_clip_number(connection, source_key)
    clip_title = _numbered_clip_title(_base_clip_title(details), number)
    duration = _duration_from_probe(probe)
    start = value("original_start_time", "comment") or "00:00:00.000"
    end = value("original_end_time", "clip_end_time") or _format_timestamp(_timestamp_milliseconds(start) + duration)
    created_at = _utc_timestamp(file_stat.st_mtime)
    revision = hashlib.sha256(f"{file_stat.st_size}:{file_stat.st_mtime_ns}".encode("ascii")).hexdigest()[:24]
    return connection.execute(
        """INSERT INTO clips (file_path, file_size, file_mtime_ns, revision, analysis_status, analysis_error, analyzed_at,
        duration_ms, media_library, media_type, title, show, season_number, episode_number, year, username,
        original_start_time, original_end_time, source_key, clip_number, clip_title, clip_title_custom, created_at,
        updated_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (public_media_path(clip_path), file_stat.st_size, file_stat.st_mtime_ns, revision,
         "error" if analysis_error else "ready", analysis_error, duration, details["media_library"], media_type,
         title, details["show"], details["season_number"], details["episode_number"], details["year"],
         value("username", "artist"), start, end, source_key, number, clip_title, 0, created_at),
    ).lastrowid


def _sync_clip(clip_path: Path) -> None:
    with lock_for_clip(clip_path):
        file_stat, public_path = clip_path.stat(), public_media_path(clip_path)
        with database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM clips WHERE file_path = ?", (public_path,)).fetchone()
            changed = row is None or row["file_size"] != file_stat.st_size or row["file_mtime_ns"] != file_stat.st_mtime_ns
            probe, probe_error = {}, ""
            if changed:
                try:
                    probe = _probe(clip_path)
                except (OSError, ffmpeg.Error) as error:
                    probe_error = str(error) or "ffprobe failed"
            if row is None:
                _insert_new_clip(connection, clip_path, probe, probe_error)
            else:
                updates = {}
                if changed:
                    updates.update({"file_size": file_stat.st_size, "file_mtime_ns": file_stat.st_mtime_ns,
                                    "revision": hashlib.sha256(f"{file_stat.st_size}:{file_stat.st_mtime_ns}".encode("ascii")).hexdigest()[:24],
                                    "analysis_status": "error" if probe_error else "ready", "analysis_error": probe_error})
                    if not probe_error:
                        updates["duration_ms"] = _duration_from_probe(probe)
                if updates:
                    _update_clip(connection, row["id"], updates, analyzed=True)


def sync_library() -> None:
    database.initialize_database()
    discovered = []
    if VIDEO_DIRECTORY.is_dir():
        for path in VIDEO_DIRECTORY.iterdir():
            try:
                if path.is_file() and path.suffix.lower() == ".mp4":
                    discovered.append((path.stat().st_mtime_ns, path.name.casefold(), path))
            except OSError:
                LOGGER.warning("Clip disappeared while scanning %s", path)
    paths = [item[2] for item in sorted(discovered)]
    present = set()
    for clip_path in paths:
        present.add(public_media_path(clip_path))
        try:
            _sync_clip(clip_path)
        except (OSError, ffmpeg.Error, ValueError):
            LOGGER.warning("Could not synchronize clip %s", clip_path, exc_info=True)
    with database.transaction(immediate=True) as connection:
        stale = [row["file_path"] for row in connection.execute("SELECT file_path FROM clips").fetchall()
                 if row["file_path"] not in present]
        connection.executemany("DELETE FROM clips WHERE file_path = ?", ((path,) for path in stale))


def _row_for_path(clip_path: Path):
    database.initialize_database()
    _sync_clip(clip_path)
    with database.open_connection() as connection:
        return connection.execute("SELECT * FROM clips WHERE file_path = ?", (public_media_path(clip_path),)).fetchone()


def _descriptor(row) -> dict:
    episode_code = _episode_code(row["season_number"], row["episode_number"])
    if row["media_type"] == "episode":
        display_heading = " · ".join(part for part in (row["show"] or "Unknown Series", episode_code) if part)
        display_subtitle = row["title"] or "Untitled episode"
    else:
        display_heading = row["title"] or "Untitled movie"
        if row["year"]:
            display_heading = f"{display_heading} ({row['year']})"
        display_subtitle = "Movie"
    immich_asset_id = row["immich_asset_id"]
    immich_asset_url = ""
    immich_can_delete = False
    if immich_asset_id:
        try:
            from app.uploaders import immich_asset_url as build_immich_asset_url
            from app import settings
            immich_asset_url = build_immich_asset_url(immich_asset_id)
            immich_can_delete = settings.get("immich_manage_assets") == "true"
        except Exception:
            pass
    return {"file_path": row["file_path"], "clip_title": row["clip_title"], "clip_number": row["clip_number"],
            "clip_title_custom": bool(row["clip_title_custom"]), "source_key": row["source_key"], "revision": row["revision"],
            "title": row["title"], "original_start_time": row["original_start_time"], "original_end_time": row["original_end_time"],
            "duration_ms": row["duration_ms"], "username": row["username"], "show": row["show"],
            "episode_number": row["episode_number"], "season_number": row["season_number"],
            "media_library": row["media_library"], "media_type": row["media_type"], "year": row["year"],
            "created_at": row["created_at"], "immich_asset_id": immich_asset_id, "immich_asset_url": immich_asset_url, "immich_can_delete": immich_can_delete, "episode_code": episode_code, "display_heading": display_heading,
            "display_subtitle": display_subtitle,
            "thumbnail_path": "/api/clips/thumbnail?file_path=" + quote(row["file_path"], safe="")}


def describe_clip(file_path, probe_data=None) -> dict:
    row = _row_for_path(_resolve_clip_path(file_path))
    if row is None:
        raise MediaFileError("The selected generated clip no longer exists.", 404)
    return _descriptor(row)


def list_clips(limit=None, sort="newest") -> list:
    if sort not in SORT_ORDERS:
        raise MediaFileError("Unsupported clip sort order.")
    sync_library()
    with database.open_connection() as connection:
        if limit is None:
            rows = connection.execute(SORT_QUERIES[sort])
        else:
            rows = connection.execute(SORT_LIMIT_QUERIES[sort], (max(0, int(limit)),))
        return [_descriptor(row) for row in rows]


def load_clip_metadata(file_path) -> dict:
    clip_path = _resolve_clip_path(file_path)
    row = _row_for_path(clip_path)
    if row is None:
        return {}
    with database.open_connection() as connection:
        return _metadata_from_row(connection, row)


def update_clip_fields(file_path, updates: dict) -> None:
    clip_path = _resolve_clip_path(file_path)
    row = _row_for_path(clip_path)
    if row is None:
        raise MediaFileError("The selected generated clip no longer exists.", 404)
    allowed = set(TEXT_FIELDS + TIMELINE_FIELDS + ("media_type", "username", "source_key", "clip_number", "clip_title", "clip_title_custom", "immich_asset_id", "created_at"))
    scalars = {key: value for key, value in updates.items() if key in allowed}
    with database.transaction(immediate=True) as connection:
        if scalars:
            _update_clip(connection, row["id"], scalars)
        if "source" in updates:
            _save_source(connection, row["id"], updates.get("source"))


def _allocated_clip_numbers(source_key: str, excluded_path=None) -> list:
    excluded_public = public_media_path(Path(excluded_path).resolve()) if excluded_path else None
    with database.open_connection() as connection:
        if excluded_public:
            rows = connection.execute("SELECT clip_number FROM clips WHERE source_key = ? AND file_path != ? ORDER BY clip_number", (source_key, excluded_public))
        else:
            rows = connection.execute("SELECT clip_number FROM clips WHERE source_key = ? ORDER BY clip_number", (source_key,))
        return [row["clip_number"] for row in rows]


def allocate_clip_title(payload: dict, exclude_file_path=None) -> dict:
    sync_library()
    source_key, excluded_id = _source_key(payload), None
    if exclude_file_path:
        excluded = _resolve_clip_path(exclude_file_path)
        with database.open_connection() as connection:
            row = connection.execute("SELECT id FROM clips WHERE file_path = ?", (public_media_path(excluded),)).fetchone()
            excluded_id = row["id"] if row else None
    with database.transaction(immediate=True) as connection:
        number = _next_clip_number(connection, source_key, excluded_id)
    return {"source_key": source_key, "clip_number": number,
            "clip_title": _numbered_clip_title(_base_clip_title(payload), number), "clip_title_custom": False}


def _validated_metadata(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise MediaFileError("Clip details must be a JSON object.")
    media_type, clip_title = _clean(payload.get("media_type")).lower(), _clean(payload.get("clip_title"))
    if media_type not in VALID_MEDIA_TYPES:
        raise MediaFileError("Media type must be movie or episode.")
    if len(clip_title) > MAX_TEXT_LENGTH:
        raise MediaFileError(f"Clip title must contain at most {MAX_TEXT_LENGTH} characters.")
    result = {"media_type": media_type, "clip_title": clip_title}
    for field in TEXT_FIELDS:
        value = _clean(payload.get(field))
        if len(value) > MAX_TEXT_LENGTH:
            raise MediaFileError(f"{field.replace('_', ' ').title()} must contain at most {MAX_TEXT_LENGTH} characters.")
        result[field] = value
    if result["year"] and not re.fullmatch(r"\d{4}", result["year"]):
        raise MediaFileError("Year must contain four digits.")
    for field in ("season_number", "episode_number"):
        if result[field] and not re.fullmatch(r"\d{1,4}", result[field]):
            raise MediaFileError(f"{field.replace('_', ' ').title()} must be a non-negative number.")
    if media_type == "movie":
        result.update({"show": "", "season_number": "", "episode_number": ""})
    else:
        result["year"] = ""
    return result


def save_clip_metadata(file_path: str, payload: dict, initialize=False) -> dict:
    clip_path = _resolve_clip_path(file_path)
    row = _row_for_path(clip_path)
    if row is None:
        raise MediaFileError("The selected generated clip no longer exists.", 404)
    with lock_for_clip(clip_path), database.transaction(immediate=True) as connection:
        current = _metadata_from_row(connection, row)
        if initialize:
            cleaned = {field: _clean(payload.get(field)) for field in TEXT_FIELDS + TIMELINE_FIELDS}
            cleaned["username"] = _clean(payload.get("username"))
            media_type = _clean(payload.get("media_type")).lower()
            cleaned["media_type"] = media_type if media_type in VALID_MEDIA_TYPES else "movie"
            source_key = _clean(payload.get("source_key")) or _source_key(cleaned)
            supplied = _positive_integer(payload.get("clip_number"), 0)
            if supplied and _number_is_available(connection, source_key, supplied, row["id"]):
                number = supplied
            elif source_key == row["source_key"]:
                number = row["clip_number"]
            else:
                number = _next_clip_number(connection, source_key, row["id"])
            supplied_title = _clean(payload.get("clip_title"))
            supplied_identity = bool(supplied and number == supplied and _clean(payload.get("source_key")) and supplied_title)
            custom = bool(payload.get("clip_title_custom")) and bool(supplied_title)
            title = supplied_title if supplied_identity else _numbered_clip_title(_base_clip_title(cleaned), number)
        else:
            existing = _descriptor(row)
            cleaned = _validated_metadata(payload)
            cleaned["username"] = current.get("username", "")
            for field in TIMELINE_FIELDS:
                cleaned[field] = _clean(current.get(field)) or _clean(existing.get(field))
            source_key = _source_key(cleaned)
            number = existing["clip_number"] if source_key == existing["source_key"] else _next_clip_number(connection, source_key, row["id"])
            requested_title = cleaned.pop("clip_title")
            custom = bool(existing["clip_title_custom"])
            if not requested_title:
                custom = False
            elif requested_title != existing["clip_title"]:
                custom = True
            title = requested_title if custom else _numbered_clip_title(_base_clip_title(cleaned), number)
        values = {field: cleaned.get(field, "") for field in TEXT_FIELDS + TIMELINE_FIELDS + ("media_type", "username")}
        values.update({"source_key": source_key, "clip_number": number, "clip_title": title, "clip_title_custom": int(custom)})
        if _clean(payload.get("created_at")):
            values["created_at"] = _clean(payload.get("created_at"))
        _update_clip(connection, row["id"], values)
        if initialize and isinstance(payload.get("source"), dict):
            _save_source(connection, row["id"], payload["source"])
    return describe_clip(clip_path)


def ensure_thumbnail(file_path: str) -> Path:
    clip_path = _resolve_clip_path(file_path)
    thumbnail_path = thumbnail_path_for_clip(clip_path)
    if thumbnail_path.is_file():
        return thumbnail_path
    THUMBNAIL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with lock_for_clip(clip_path):
        if thumbnail_path.is_file():
            return thumbnail_path
        temporary_path = thumbnail_path.with_name(thumbnail_path.stem + ".tmp.jpg")
        try:
            frame = ffmpeg.input(str(clip_path), ss=0.25).video.filter("scale", 720, -2)
            ffmpeg.output(frame, str(temporary_path), format="image2", vframes=1, vcodec="mjpeg", **{"q:v": 3}).overwrite_output().run(capture_stdout=True, capture_stderr=True)
            os.replace(temporary_path, thumbnail_path)
        except (OSError, ffmpeg.Error) as error:
            temporary_path.unlink(missing_ok=True)
            raise MediaFileError("A preview image could not be generated for this clip.", 422) from error
    return thumbnail_path
