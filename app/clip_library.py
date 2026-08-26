import json
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import ffmpeg

from app.media_files import (
    MediaFileError,
    THUMBNAIL_DIRECTORY,
    VIDEO_DIRECTORY,
    lock_for_clip,
    legacy_metadata_path_for_clip,
    metadata_path_for_clip,
    public_media_path,
    resolve_generated_clip,
    thumbnail_path_for_clip,
)


TEXT_FIELDS = ("media_library", "title", "show", "season_number", "episode_number", "year")
TIMELINE_FIELDS = ("original_start_time", "original_end_time")
VALID_MEDIA_TYPES = {"movie", "episode"}
MAX_TEXT_LENGTH = 255


def _utc_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _read_sidecar(clip_path: Path) -> dict:
    metadata_path = metadata_path_for_clip(clip_path)
    legacy_path = legacy_metadata_path_for_clip(clip_path)
    if not metadata_path.is_file() and legacy_path.is_file():
        try:
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy_path, metadata_path)
        except OSError:
            if not metadata_path.is_file():
                metadata_path = legacy_path
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _probe(clip_path: Path) -> dict:
    probe = ffmpeg.probe(str(clip_path))
    file_format = probe.get("format") if isinstance(probe, dict) else {}
    return file_format if isinstance(file_format, dict) else {}


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _four_digit_year(value) -> str:
    match = re.search(r"(?:^|\D)(\d{4})(?:\D|$)", _clean(value))
    return match.group(1) if match else ""


def _episode_code(season, episode) -> str:
    season_value = _clean(season)
    episode_value = _clean(episode)
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


def _source_key(details: dict) -> str:
    media_type = _clean(details.get("media_type")).lower()
    library = (_clean(details.get("media_library")) or "Uncategorized").casefold()
    if media_type == "episode":
        season = _clean(details.get("season_number"))
        episode = _clean(details.get("episode_number"))
        season = str(int(season)) if season.isdigit() else season.casefold()
        episode = str(int(episode)) if episode.isdigit() else episode.casefold()
        identity = ["episode", library, _clean(details.get("show")).casefold(), season, episode]
    else:
        identity = ["movie", library, _clean(details.get("title")).casefold(), _four_digit_year(details.get("year"))]
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _base_clip_title(details: dict) -> str:
    if _clean(details.get("media_type")).lower() == "episode":
        parts = [
            _clean(details.get("show")) or "Unknown Series",
            _episode_code(details.get("season_number"), details.get("episode_number")),
            _clean(details.get("title")) or "Untitled episode",
        ]
        return " - ".join(part for part in parts if part)
    title = _clean(details.get("title")) or "Untitled movie"
    year = _four_digit_year(details.get("year"))
    return f"{title} ({year})" if year else title


def _numbered_clip_title(base_title: str, clip_number: int) -> str:
    number = _positive_integer(clip_number)
    suffix = f" - {number}" if number > 1 else ""
    available = MAX_TEXT_LENGTH - len(suffix)
    base = (_clean(base_title) or "Clip")[:available].rstrip()
    return f"{base}{suffix}"


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


def _sort_time(created_at: str, fallback: float) -> float:
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return fallback


def _resolve_clip_path(file_path) -> Path:
    candidate = Path(file_path)
    if candidate.is_absolute():
        return resolve_generated_clip(public_media_path(candidate))
    normalized = candidate.as_posix()
    if normalized.startswith("app/"):
        normalized = normalized[4:]
    return resolve_generated_clip(normalized)


def describe_clip(file_path, probe_data=None) -> dict:
    clip_path = _resolve_clip_path(file_path)
    file_format = probe_data if isinstance(probe_data, dict) else _probe(clip_path)
    tags = file_format.get("tags") if isinstance(file_format.get("tags"), dict) else {}
    sidecar = _read_sidecar(clip_path)

    def value(key, *fallback_keys):
        if key in sidecar:
            return _clean(sidecar.get(key))
        for fallback_key in (key,) + fallback_keys:
            if _clean(tags.get(fallback_key)):
                return _clean(tags.get(fallback_key))
        return ""

    title = value("title")
    show = value("show")
    season_number = value("season_number")
    episode_number = value("episode_number", "episode_id")
    inferred_type = "episode" if show or season_number or episode_number else "movie"
    media_type = value("media_type").lower() or inferred_type
    if media_type not in VALID_MEDIA_TYPES:
        media_type = inferred_type
    year = _four_digit_year(value("year", "date"))
    media_library = value("media_library", "album") or "Uncategorized"
    created_at = _clean(sidecar.get("created_at")) or _utc_timestamp(clip_path.stat().st_mtime)
    original_start_time = value("original_start_time", "comment") or "00:00:00.000"
    original_end_time = value("original_end_time", "clip_end_time")
    try:
        duration_ms = max(0, round(float(file_format.get("duration") or 0) * 1000))
    except (TypeError, ValueError):
        duration_ms = 0
    if not original_end_time:
        original_end_time = _format_timestamp(_timestamp_milliseconds(original_start_time) + duration_ms)
    episode_code = _episode_code(season_number, episode_number)
    if media_type == "episode":
        display_heading = " · ".join(part for part in (show or "Unknown Series", episode_code) if part)
        display_subtitle = title or "Untitled episode"
    else:
        display_heading = title or "Untitled movie"
        if year:
            display_heading = f"{display_heading} ({year})"
        display_subtitle = "Movie"

    source_details = {
        "media_library": media_library,
        "media_type": media_type,
        "title": title,
        "show": show,
        "season_number": season_number,
        "episode_number": episode_number,
        "year": year,
    }
    stored_clip_title = value("clip_title")
    stored_clip_number = sidecar.get("clip_number")
    clip_number = _positive_integer(stored_clip_number)
    clip_title_custom = bool(sidecar.get("clip_title_custom")) and bool(stored_clip_title)
    clip_title = stored_clip_title or _numbered_clip_title(_base_clip_title(source_details), clip_number)

    public_path = public_media_path(clip_path)
    clip_stat = clip_path.stat()
    revision = hashlib.sha256(f"{clip_stat.st_size}:{clip_stat.st_mtime_ns}".encode("ascii")).hexdigest()[:24]
    descriptor = {
        "file_path": public_path,
        "clip_title": clip_title,
        "clip_number": clip_number,
        "clip_title_custom": clip_title_custom,
        "source_key": _source_key(source_details),
        "revision": revision,
        "title": title,
        "original_start_time": original_start_time,
        "original_end_time": original_end_time,
        "duration_ms": duration_ms,
        "username": value("username", "artist"),
        "show": show,
        "episode_number": episode_number,
        "season_number": season_number,
        "media_library": media_library,
        "media_type": media_type,
        "year": year,
        "created_at": created_at,
        "episode_code": episode_code,
        "display_heading": display_heading,
        "display_subtitle": display_subtitle,
        "thumbnail_path": "/api/clips/thumbnail?file_path=" + quote(public_path, safe=""),
    }
    return descriptor


def list_clips(limit=None) -> list:
    clips = []
    if not VIDEO_DIRECTORY.is_dir():
        return clips
    for clip_path in VIDEO_DIRECTORY.iterdir():
        if clip_path.suffix.lower() != ".mp4" or not clip_path.is_file():
            continue
        try:
            clips.append(describe_clip(clip_path))
        except (MediaFileError, OSError, ffmpeg.Error):
            continue

    groups = {}
    for clip in clips:
        groups.setdefault(clip["source_key"], []).append(clip)
    for source_clips in groups.values():
        source_clips.sort(key=lambda clip: (_sort_time(clip.get("created_at"), 0), clip["file_path"]))
        used_numbers = set()
        next_number = 1
        for clip in source_clips:
            sidecar = _read_sidecar(_resolve_clip_path(clip["file_path"]))
            stored_number = _positive_integer(sidecar.get("clip_number"), 0)
            if stored_number > 0 and stored_number not in used_numbers:
                number = stored_number
            else:
                while next_number in used_numbers:
                    next_number += 1
                number = next_number
            used_numbers.add(number)
            next_number = max(next_number, number + 1)
            inferred_title = _numbered_clip_title(_base_clip_title(clip), number)
            clip_title = clip["clip_title"] if clip["clip_title_custom"] else inferred_title
            updates = {
                "source_key": clip["source_key"],
                "clip_number": number,
                "clip_title": clip_title,
                "clip_title_custom": clip["clip_title_custom"],
            }
            try:
                if any(sidecar.get(key) != value for key, value in updates.items()):
                    _update_sidecar(clip["file_path"], updates, clip.get("created_at"))
            except OSError:
                pass
            clip.update(updates)
    clips.sort(key=lambda clip: _sort_time(clip.get("created_at"), 0), reverse=True)
    return clips[:limit] if limit is not None else clips


def _update_sidecar(file_path, updates: dict, created_at=None) -> None:
    clip_path = _resolve_clip_path(file_path)
    metadata_path = metadata_path_for_clip(clip_path)
    with lock_for_clip(clip_path):
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        current = _read_sidecar(clip_path)
        current.update(updates)
        current["version"] = 1
        current["created_at"] = _clean(current.get("created_at")) or _clean(created_at) or _utc_timestamp(clip_path.stat().st_mtime)
        temporary_path = metadata_path.with_name(metadata_path.name + ".tmp")
        temporary_path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary_path, metadata_path)


def allocate_clip_title(payload: dict, exclude_file_path=None) -> dict:
    source_key = _source_key(payload)
    excluded = None
    if exclude_file_path:
        excluded = public_media_path(_resolve_clip_path(exclude_file_path))
    numbers = [
        _positive_integer(clip.get("clip_number"))
        for clip in list_clips()
        if clip.get("source_key") == source_key and clip.get("file_path") != excluded
    ]
    clip_number = max(numbers, default=0) + 1
    return {
        "source_key": source_key,
        "clip_number": clip_number,
        "clip_title": _numbered_clip_title(_base_clip_title(payload), clip_number),
        "clip_title_custom": False,
    }


def _validated_metadata(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise MediaFileError("Clip details must be a JSON object.")
    media_type = _clean(payload.get("media_type")).lower()
    if media_type not in VALID_MEDIA_TYPES:
        raise MediaFileError("Media type must be movie or episode.")
    clip_title = _clean(payload.get("clip_title"))
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
    metadata_path = metadata_path_for_clip(clip_path)
    with lock_for_clip(clip_path):
        current = _read_sidecar(clip_path)
        if initialize:
            cleaned = {field: _clean(payload.get(field)) for field in TEXT_FIELDS + TIMELINE_FIELDS}
            media_type = _clean(payload.get("media_type")).lower()
            cleaned["media_type"] = media_type if media_type in VALID_MEDIA_TYPES else "movie"
            identity = allocate_clip_title(cleaned, clip_path)
            for field in ("source_key", "clip_number", "clip_title", "clip_title_custom"):
                cleaned[field] = payload.get(field, identity[field])
            cleaned["clip_number"] = _positive_integer(cleaned["clip_number"])
            cleaned["clip_title"] = _clean(cleaned["clip_title"]) or identity["clip_title"]
            cleaned["clip_title_custom"] = bool(cleaned["clip_title_custom"])
            if isinstance(payload.get("source"), dict):
                cleaned["source"] = payload["source"]
        else:
            existing = describe_clip(clip_path)
            cleaned = _validated_metadata(payload)
            for field in TIMELINE_FIELDS:
                cleaned[field] = _clean(current.get(field)) or _clean(existing.get(field))
            source_key = _source_key(cleaned)
            if source_key == existing.get("source_key"):
                clip_number = _positive_integer(existing.get("clip_number"))
            else:
                clip_number = allocate_clip_title(cleaned, clip_path)["clip_number"]
            requested_title = cleaned.pop("clip_title")
            if not requested_title:
                clip_title_custom = False
            elif requested_title != existing.get("clip_title"):
                clip_title_custom = True
            else:
                clip_title_custom = bool(existing.get("clip_title_custom"))
            cleaned.update({
                "source_key": source_key,
                "clip_number": clip_number,
                "clip_title": requested_title if clip_title_custom else _numbered_clip_title(_base_clip_title(cleaned), clip_number),
                "clip_title_custom": clip_title_custom,
            })
            if isinstance(current.get("source"), dict):
                cleaned["source"] = current["source"]
        cleaned["version"] = 1
        cleaned["created_at"] = _clean(current.get("created_at")) or _clean(payload.get("created_at")) or _utc_timestamp(clip_path.stat().st_mtime)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = metadata_path.with_name(metadata_path.name + ".tmp")
        temporary_path.write_text(json.dumps(cleaned, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary_path, metadata_path)
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
            ffmpeg.output(
                frame,
                str(temporary_path),
                format="image2",
                vframes=1,
                vcodec="mjpeg",
                **{"q:v": 3},
            ).overwrite_output().run(capture_stdout=True, capture_stderr=True)
            os.replace(temporary_path, thumbnail_path)
        except (OSError, ffmpeg.Error) as error:
            temporary_path.unlink(missing_ok=True)
            raise MediaFileError("A preview image could not be generated for this clip.", 422) from error
    return thumbnail_path
