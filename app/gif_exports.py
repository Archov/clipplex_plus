import os
from pathlib import Path
import uuid

import ffmpeg

import clipplexAPI
from app.media_files import (
    GIF_DIRECTORY,
    MediaFileError,
    gif_path_for_clip,
    public_media_path,
    resolve_generated_clip,
)


MAX_GIF_BYTES = 9_500_000
GIF_PROFILES = (
    {"max_dimension": 720, "fps": 15, "colors": 128},
    {"max_dimension": 640, "fps": 12, "colors": 96},
    {"max_dimension": 540, "fps": 10, "colors": 80},
    {"max_dimension": 480, "fps": 8, "colors": 64},
    {"max_dimension": 360, "fps": 6, "colors": 48},
)


class GifExportError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _resolve_clip(file_path: str) -> Path:
    try:
        return resolve_generated_clip(file_path)
    except MediaFileError as error:
        raise GifExportError(error.message, error.status_code) from error


def _is_valid_cache(clip_path: Path, gif_path: Path) -> bool:
    try:
        clip_stat = clip_path.stat()
        gif_stat = gif_path.stat()
    except OSError:
        return False
    return (
        gif_path.is_file()
        and 0 < gif_stat.st_size <= MAX_GIF_BYTES
        and gif_stat.st_mtime_ns >= clip_stat.st_mtime_ns
    )


def _descriptor(gif_path: Path, cached: bool) -> dict:
    relative_path = public_media_path(gif_path)
    return {
        "file_path": relative_path,
        "download_url": f"/{relative_path}",
        "filename": gif_path.name,
        "size_bytes": gif_path.stat().st_size,
        "cached": cached,
    }


def cached_export(file_path: str):
    clip_path = _resolve_clip(file_path)
    gif_path = gif_path_for_clip(clip_path)
    if _is_valid_cache(clip_path, gif_path):
        return _descriptor(gif_path, True)
    return None


def _duration_seconds(clip_path: Path) -> float:
    try:
        probe = ffmpeg.probe(str(clip_path))
        duration = float((probe.get("format") or {}).get("duration") or 0)
    except (ffmpeg.Error, OSError, TypeError, ValueError) as error:
        raise GifExportError("FFmpeg could not inspect the saved clip.") from error
    if duration <= 0:
        raise GifExportError("The saved clip does not have a usable duration.")
    return duration


def build_gif_graph(clip_path: Path, output_path: Path, profile: dict):
    source = ffmpeg.input(str(clip_path)).video
    max_dimension = profile["max_dimension"]
    scaled = (
        source
        .filter("fps", fps=profile["fps"])
        .filter(
            "scale",
            f"min({max_dimension},iw)",
            f"min({max_dimension},ih)",
            force_original_aspect_ratio="decrease",
            flags="lanczos",
        )
        .filter("setsar", 1)
    )
    split = scaled.filter_multi_output("split")
    palette = split[0].filter(
        "palettegen",
        max_colors=profile["colors"],
        stats_mode="diff",
    )
    gif = ffmpeg.filter(
        [split[1], palette],
        "paletteuse",
        dither="bayer",
        bayer_scale=3,
        diff_mode="rectangle",
    )
    return ffmpeg.output(gif, str(output_path), format="gif", loop=0).overwrite_output()


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def export_gif(file_path: str, progress_callback=None) -> dict:
    clip_path = _resolve_clip(file_path)
    gif_path = gif_path_for_clip(clip_path)
    if _is_valid_cache(clip_path, gif_path):
        return {"result": "success", "export": _descriptor(gif_path, True)}

    GIF_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if gif_path.exists():
        try:
            gif_path.unlink()
        except OSError as error:
            raise GifExportError("The stale GIF cache could not be replaced.") from error

    duration = _duration_seconds(clip_path)
    temporary_path = GIF_DIRECTORY / f".{gif_path.stem}.{uuid.uuid4().hex}.tmp.gif"

    def emit(stage, overall, stage_progress, message):
        if progress_callback is not None:
            progress_callback(stage, overall, stage_progress, message)

    try:
        emit("preparing_gif", 3, 100, "Preparing the saved clip for GIF conversion.")
        profile_count = len(GIF_PROFILES)
        for index, profile in enumerate(GIF_PROFILES):
            _remove_temporary(temporary_path)
            attempt_start = 5 + index * (90 / profile_count)
            attempt_span = 90 / profile_count
            message = f"Rendering share-ready GIF (quality pass {index + 1}/{profile_count})."
            emit("rendering_gif", attempt_start, 0, message)
            clipplexAPI.run_ffmpeg_with_progress(
                build_gif_graph(clip_path, temporary_path, profile),
                duration,
                lambda percent, start=attempt_start, span=attempt_span, text=message: emit(
                    "rendering_gif", start + span * percent / 100, percent, text
                ),
            )
            if not clip_path.is_file():
                raise GifExportError("The source clip was deleted during GIF export.", 404)
            if temporary_path.is_file() and 0 < temporary_path.stat().st_size <= MAX_GIF_BYTES:
                emit("finalizing_gif", 97, 50, "Caching the completed GIF.")
                os.replace(temporary_path, gif_path)
                emit("finalizing_gif", 99, 100, "The GIF is ready to download.")
                return {"result": "success", "export": _descriptor(gif_path, False)}

        raise GifExportError(
            "This clip cannot fit under the 9.5 MB share limit. Create a shorter clip and try again."
        )
    except GifExportError:
        raise
    except ffmpeg.Error as error:
        raise GifExportError("FFmpeg could not convert this clip to a GIF.") from error
    except OSError as error:
        raise GifExportError("Clipplex could not save the generated GIF.") from error
    finally:
        _remove_temporary(temporary_path)
