import os
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

import ffmpeg

import clipplexAPI
from app import clip_library
from app.media_files import (
    GIF_DIRECTORY,
    PREVIEW_DIRECTORY,
    THUMBNAIL_DIRECTORY,
    VIDEO_DIRECTORY,
    WORK_DIRECTORY,
    MediaFileError,
    gif_path_for_clip,
    lock_for_clip,
    public_media_path,
    resolve_generated_clip,
    thumbnail_path_for_clip,
)


MIN_TRIM_DURATION_MS = 100
PREVIEW_CONTEXT_MS = 30_000
PREVIEW_RETENTION_SECONDS = 3_600


class ClipTrimError(MediaFileError):
    pass


def _timestamp_ms(value) -> int:
    return clip_library._timestamp_milliseconds(str(value or ""))


def _timestamp(value) -> str:
    return clip_library._format_timestamp(int(value))


def _track_payload(track):
    if track is None:
        return None
    return {
        key: value
        for key, value in asdict(track).items()
        if key in {
            "id", "index", "track_type", "codec", "language", "title", "key",
            "selected", "available", "unavailable_reason", "subtitle_index", "probe_codec",
        }
    }


def _track_from_payload(payload):
    if not isinstance(payload, dict):
        return None
    allowed = set(clipplexAPI.MediaTrack.__dataclass_fields__)
    return clipplexAPI.MediaTrack(**{key: value for key, value in payload.items() if key in allowed})


def _file_fingerprint(path: Path) -> dict:
    file_stat = path.stat()
    return {"size": file_stat.st_size, "mtime_ns": file_stat.st_mtime_ns}


def _duration_from_probe(probe: dict) -> int:
    try:
        return max(0, round(float((probe.get("format") or {}).get("duration") or 0) * 1000))
    except (TypeError, ValueError):
        return 0


def build_source_provenance(plex_data, audio_track, subtitle_track) -> dict:
    source_path = Path(plex_data.media_path).resolve()
    probe = getattr(plex_data, "probe", None) or ffmpeg.probe(str(source_path))
    duration_ms = _duration_from_probe(probe) or int(getattr(plex_data, "duration_ms", 0) or 0)
    return {
        "version": 1,
        "rating_key": plex_data.metadata_element.attrib.get("ratingKey", ""),
        "media_key": getattr(plex_data, "media_key", ""),
        "part_id": getattr(getattr(plex_data, "metadata_part", None), "attrib", {}).get("id", ""),
        "media_path": str(source_path),
        "duration_ms": duration_ms,
        "fingerprint": _file_fingerprint(source_path),
        "video_stream_index": getattr(plex_data, "video_index", 0),
        "audio_track": _track_payload(audio_track),
        "subtitle_track": _track_payload(subtitle_track),
    }


def _source_is_current(source: dict) -> bool:
    if not isinstance(source, dict) or not source.get("media_path"):
        return False
    try:
        path = Path(source["media_path"]).resolve()
        return path.is_file() and _file_fingerprint(path) == source.get("fingerprint")
    except OSError:
        return False


def _require_saved_source(file_path) -> dict:
    source = _clip_private_metadata(file_path).get("source")
    if not isinstance(source, dict) or not source.get("media_path") or not source.get("fingerprint"):
        raise ClipTrimError(
            "This clip does not have saved original-source metadata, so it cannot be extended.",
            422,
        )
    try:
        source_path = Path(source["media_path"]).resolve()
        if not source_path.is_file():
            raise ClipTrimError(
                "The original Plex media is no longer available. It may have been deleted from Plex or is not mounted in Clipplex.",
                422,
            )
        if _file_fingerprint(source_path) != source.get("fingerprint"):
            raise ClipTrimError(
                "The original Plex media has changed since this clip was created, so it cannot be extended safely.",
                409,
            )
    except ClipTrimError:
        raise
    except OSError as error:
        raise ClipTrimError("The original Plex media could not be accessed.", 422) from error
    return source


def _clip_private_metadata(file_path) -> dict:
    return clip_library.load_clip_metadata(file_path)


def _stored_track_options(source: dict) -> dict:
    audio = _track_from_payload(source.get("audio_track"))
    subtitle = _track_from_payload(source.get("subtitle_track"))
    return {
        "audio": [audio.as_option()] if audio else [],
        "subtitles": [{
            "id": "none", "label": "Off", "selected": subtitle is None,
            "available": True, "unavailable_reason": "",
        }] + ([subtitle.as_option()] if subtitle else []),
    }


def source_options(file_path) -> dict:
    source = _require_saved_source(file_path)
    return {
        "status": "ready",
        "source_duration_ms": int(source.get("duration_ms") or 0),
        "tracks": _stored_track_options(source),
    }


def _resolve_provenance(file_path, audio_stream_id=None, subtitle_stream_id=None) -> dict:
    source = dict(_require_saved_source(file_path))
    audio = _track_from_payload(source.get("audio_track"))
    if audio is None or (audio_stream_id and str(audio.id) != str(audio_stream_id)):
        raise ClipTrimError("The saved original audio track is no longer available.", 422)
    subtitle = _track_from_payload(source.get("subtitle_track"))
    if subtitle_stream_id == "none":
        source["subtitle_track"] = None
    elif subtitle_stream_id and (subtitle is None or str(subtitle.id) != str(subtitle_stream_id)):
        raise ClipTrimError("The saved original subtitle track is no longer available.", 422)
    return source


class SavedSource:
    def __init__(self, source: dict, clip: dict, start_ms=0):
        self.media_path = source["media_path"]
        self.probe = ffmpeg.probe(self.media_path)
        self.video_index = int(source.get("video_stream_index") or 0)
        self.media_type = clip.get("media_type", "movie")
        self.media_library = clip.get("media_library", "")
        self.media_year = clip.get("year", "")
        self.media_title = clip.get("display_heading", "")
        self.username = clip.get("username", "Unknown user")
        self.duration_ms = int(source.get("duration_ms") or _duration_from_probe(self.probe))
        self.current_media_time_str = _timestamp(start_ms)
        attributes = {
            "type": self.media_type,
            "title": clip.get("title", ""),
            "year": clip.get("year", ""),
            "grandparentTitle": clip.get("show", ""),
            "parentIndex": clip.get("season_number", ""),
            "index": clip.get("episode_number", ""),
        }
        self.metadata_element = ET.Element("Video", {key: str(value) for key, value in attributes.items() if value != ""})
        self.metadata_part = self.session_part = None
        from app import settings
        self.plex_url = settings.get("plex_url").rstrip("/")
        self.headers = {"X-Plex-Token": settings.get("plex_token")}


def _render_from_source(source: dict, clip: dict, seek_start_ms: int, metadata_start_ms: int, end_ms: int, output_path: Path, progress, preview=False, output_title=None):
    source_media = SavedSource(source, clip, seek_start_ms)
    audio = _track_from_payload(source.get("audio_track"))
    subtitle = _track_from_payload(source.get("subtitle_track"))
    if audio is None:
        raise ClipTrimError("The original audio track information is unavailable.", 422)
    video = clipplexAPI.Video(
        source_media,
        seek_start_ms,
        (end_ms - metadata_start_ms) / 1000.0,
        output_path.stem,
        audio,
        subtitle,
    )
    video.output_path = str(output_path)
    video.metadata_current_media_time = _timestamp(metadata_start_ms)
    video.metadata_end_time = _timestamp(end_ms)
    video.metadata_clip_title = output_title or clip.get("clip_title") or clip.get("display_heading") or "Clip"
    if preview:
        video.output_max_width = 1280
        video.output_max_height = 720
        video.output_crf = 28
        video.output_preset = "ultrafast"
    video.extract_video(progress)


def _clip_as_source(clip_path: Path, clip: dict) -> dict:
    probe = ffmpeg.probe(str(clip_path))
    video_stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if video_stream is None or audio_stream is None:
        raise ClipTrimError("The saved clip does not contain playable video and audio streams.", 422)
    return {
        "media_path": str(clip_path),
        "duration_ms": clip.get("duration_ms", 0),
        "video_stream_index": int(video_stream["index"]),
        "audio_track": _track_payload(clipplexAPI.MediaTrack("audio", int(audio_stream["index"]), "audio")),
        "subtitle_track": None,
    }


def validate_trim_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ClipTrimError("The trim request must contain a JSON object.")
    clip_path = resolve_generated_clip(payload.get("file_path"))
    clip = clip_library.describe_clip(clip_path)
    expected_revision = str(payload.get("expected_revision") or "")
    if not expected_revision or expected_revision != clip.get("revision"):
        raise ClipTrimError("This clip changed after the editor was opened. Refresh it and try again.", 409)
    try:
        start_ms = int(payload.get("start_ms"))
        end_ms = int(payload.get("end_ms"))
    except (TypeError, ValueError) as error:
        raise ClipTrimError("Start and end must be millisecond timestamps.") from error
    if start_ms < 0 or end_ms - start_ms < MIN_TRIM_DURATION_MS:
        raise ClipTrimError(f"The selected range must be at least {MIN_TRIM_DURATION_MS} milliseconds long.")
    mode = str(payload.get("mode") or "")
    basis = str(payload.get("basis") or "clip")
    if mode not in {"new", "replace"}:
        raise ClipTrimError("Trim mode must be new or replace.")
    if basis not in {"clip", "original"}:
        raise ClipTrimError("Trim source must be the saved clip or original media.")
    if basis == "clip" and end_ms > int(clip.get("duration_ms") or 0):
        raise ClipTrimError("The trim range must stay within the saved clip.")
    custom_title = str(payload.get("custom_title") or "").strip()
    if len(custom_title) > clip_library.MAX_TEXT_LENGTH:
        raise ClipTrimError("Clip title must contain at most 255 characters.")
    return {
        "job_type": "clip_trim",
        "file_path": public_media_path(clip_path),
        "expected_revision": expected_revision,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "mode": mode,
        "basis": basis,
        "custom_title": custom_title,
    }


def _organization_fields(clip: dict) -> dict:
    return {key: clip.get(key, "") for key in clip_library.TEXT_FIELDS + ("media_type",)}


def _refresh_companions(clip_path: Path):
    gif_path_for_clip(clip_path).unlink(missing_ok=True)
    thumbnail_path_for_clip(clip_path).unlink(missing_ok=True)
    try:
        clip_library.ensure_thumbnail(public_media_path(clip_path))
    except MediaFileError:
        pass


def run_trim_job(payload: dict, progress) -> dict:
    clip_path = resolve_generated_clip(payload.get("file_path"))
    with lock_for_clip(clip_path):
        clip = clip_library.describe_clip(clip_path)
        if clip.get("revision") != payload.get("expected_revision"):
            raise ClipTrimError("This clip changed before trimming began. Refresh it and try again.", 409)
        basis = payload["basis"]
        if basis == "clip":
            source = _clip_as_source(clip_path, clip)
            seek_start_ms = payload["start_ms"]
            absolute_start_ms = _timestamp_ms(clip.get("original_start_time")) + payload["start_ms"]
            absolute_end_ms = _timestamp_ms(clip.get("original_start_time")) + payload["end_ms"]
        else:
            source = _require_saved_source(payload["file_path"])
            if payload["end_ms"] > int(source.get("duration_ms") or 0):
                raise ClipTrimError("The selected range is outside the original media.")
            seek_start_ms = absolute_start_ms = payload["start_ms"]
            absolute_end_ms = payload["end_ms"]
        WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
        temporary_path = WORK_DIRECTORY / f"trim-{uuid.uuid4().hex}.mp4"
        identity = None
        if payload["mode"] == "new":
            identity = clip_library.allocate_clip_title(clip)
            if payload.get("custom_title"):
                identity["clip_title"] = payload["custom_title"]
                identity["clip_title_custom"] = True
        try:
            _render_from_source(
                source, clip, seek_start_ms, absolute_start_ms, absolute_end_ms,
                temporary_path, progress, output_title=identity["clip_title"] if identity else None,
            )
            if payload["mode"] == "replace":
                os.replace(temporary_path, clip_path)
                clip_library.update_clip_fields(payload["file_path"], {
                    "original_start_time": _timestamp(absolute_start_ms),
                    "original_end_time": _timestamp(absolute_end_ms),
                })
                _refresh_companions(clip_path)
                result_clip = clip_library.describe_clip(clip_path)
            else:
                new_path = VIDEO_DIRECTORY / f"{clip_path.stem}-trim-{int(time.time())}-{uuid.uuid4().hex[:8]}.mp4"
                os.replace(temporary_path, new_path)
                try:
                    result_clip = clip_library.save_clip_metadata(new_path, {
                        **_organization_fields(clip),
                        **identity,
                        "username": clip.get("username", ""),
                        "original_start_time": _timestamp(absolute_start_ms),
                        "original_end_time": _timestamp(absolute_end_ms),
                        "source": _clip_private_metadata(payload["file_path"]).get("source"),
                    }, initialize=True)
                except Exception:
                    new_path.unlink(missing_ok=True)
                    raise
                _refresh_companions(new_path)
            return {"result": "success", "operation": payload["mode"], "clip": result_clip}
        finally:
            temporary_path.unlink(missing_ok=True)


def validate_extension_preview_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ClipTrimError("The extension preview request must contain a JSON object.")
    if payload.get("source_id"):
        raise ClipTrimError("Selecting a replacement Plex source is not supported.")
    clip_path = resolve_generated_clip(payload.get("file_path"))
    clip = clip_library.describe_clip(clip_path)
    if str(payload.get("expected_revision") or "") != clip.get("revision"):
        raise ClipTrimError("This clip changed after the editor was opened. Refresh it and try again.", 409)
    _require_saved_source(public_media_path(clip_path))
    result = {
        "job_type": "extension_preview",
        "file_path": public_media_path(clip_path),
        "expected_revision": clip["revision"],
        "audio_stream_id": payload.get("audio_stream_id"),
        "subtitle_stream_id": payload.get("subtitle_stream_id"),
    }
    for key in ("window_start_ms", "window_end_ms"):
        value = payload.get(key)
        result[key] = int(value) if value is not None and value != "" else None
    for key in ("selection_start_ms", "selection_end_ms"):
        value = payload.get(key)
        result[key] = int(value) if value is not None and value != "" else None
    return result


def cleanup_previews(now=None):
    PREVIEW_DIRECTORY.mkdir(parents=True, exist_ok=True)
    cutoff = (time.time() if now is None else now) - PREVIEW_RETENTION_SECONDS
    for path in PREVIEW_DIRECTORY.glob("*.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def run_extension_preview_job(payload: dict, progress) -> dict:
    clip_path = resolve_generated_clip(payload.get("file_path"))
    with lock_for_clip(clip_path):
        clip = clip_library.describe_clip(clip_path)
        if clip.get("revision") != payload.get("expected_revision"):
            raise ClipTrimError("This clip changed before preview generation began.", 409)
        source = _resolve_provenance(
            payload["file_path"], payload.get("audio_stream_id"), payload.get("subtitle_stream_id")
        )
        source_duration = int(source.get("duration_ms") or 0)
        original_start = _timestamp_ms(clip.get("original_start_time"))
        original_end = _timestamp_ms(clip.get("original_end_time"))
        window_start = payload.get("window_start_ms")
        window_end = payload.get("window_end_ms")
        window_start = max(0, original_start - PREVIEW_CONTEXT_MS) if window_start is None else max(0, window_start)
        window_end = min(source_duration, original_end + PREVIEW_CONTEXT_MS) if window_end is None else min(source_duration, window_end)
        if window_end - window_start < MIN_TRIM_DURATION_MS:
            raise ClipTrimError("The original-source preview range is empty.")
        cleanup_previews()
        PREVIEW_DIRECTORY.mkdir(parents=True, exist_ok=True)
        preview_path = PREVIEW_DIRECTORY / f"{clip_path.stem}-{uuid.uuid4().hex}.mp4"
        try:
            _render_from_source(source, clip, window_start, window_start, window_end, preview_path, progress, preview=True)
        except Exception:
            preview_path.unlink(missing_ok=True)
            raise
        selection_start = payload.get("selection_start_ms")
        selection_end = payload.get("selection_end_ms")
        selection_start = original_start if selection_start is None else selection_start
        selection_end = original_end if selection_end is None else selection_end
        return {
            "result": "success",
            "preview": {
                "url": "/" + public_media_path(preview_path),
                "window_start_ms": window_start,
                "window_end_ms": window_end,
                "source_duration_ms": source_duration,
                "selection_start_ms": max(window_start, min(selection_start, window_end - MIN_TRIM_DURATION_MS)),
                "selection_end_ms": min(window_end, max(selection_end, window_start + MIN_TRIM_DURATION_MS)),
            },
        }


cleanup_previews()
