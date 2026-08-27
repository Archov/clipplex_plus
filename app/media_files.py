from pathlib import Path
import threading


APP_DIRECTORY = Path(__file__).resolve().parent
MEDIA_DIRECTORY = (APP_DIRECTORY / "static" / "media").resolve()
VIDEO_DIRECTORY = (MEDIA_DIRECTORY / "videos").resolve()
GIF_DIRECTORY = (MEDIA_DIRECTORY / "gifs").resolve()
THUMBNAIL_DIRECTORY = (MEDIA_DIRECTORY / "thumbnails").resolve()
PREVIEW_DIRECTORY = (MEDIA_DIRECTORY / "previews").resolve()
PRIVATE_MEDIA_DIRECTORY = (MEDIA_DIRECTORY / ".clipplex").resolve()
CLIP_METADATA_DIRECTORY = (PRIVATE_MEDIA_DIRECTORY / "metadata").resolve()
WORK_DIRECTORY = (PRIVATE_MEDIA_DIRECTORY / "work").resolve()
CLIP_METADATA_SUFFIX = ".clipplex.json"
_CLIP_LOCKS = {}
_CLIP_LOCKS_GUARD = threading.Lock()


class MediaFileError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_generated_clip(file_path: str) -> Path:
    if not isinstance(file_path, str) or not file_path.strip():
        raise MediaFileError("Select a generated clip.")
    relative_path = file_path.strip().lstrip("/\\")
    candidate = (APP_DIRECTORY / relative_path).resolve()
    try:
        candidate.relative_to(VIDEO_DIRECTORY)
    except ValueError as error:
        raise MediaFileError("Only generated Clipplex videos may be used.") from error
    if candidate.suffix.lower() != ".mp4":
        raise MediaFileError("Only generated MP4 clips may be used.")
    if not candidate.is_file():
        raise MediaFileError("The selected generated clip no longer exists.", 404)
    return candidate


def gif_path_for_clip(clip_path: Path) -> Path:
    return GIF_DIRECTORY / f"{clip_path.stem}.gif"


def thumbnail_path_for_clip(clip_path: Path) -> Path:
    return THUMBNAIL_DIRECTORY / f"{clip_path.stem}.jpg"


def metadata_path_for_clip(clip_path: Path) -> Path:
    return CLIP_METADATA_DIRECTORY / f"{clip_path.stem}.json"


def legacy_metadata_path_for_clip(clip_path: Path) -> Path:
    return clip_path.with_suffix(CLIP_METADATA_SUFFIX)


def preview_paths_for_clip(clip_path: Path):
    return PREVIEW_DIRECTORY.glob(f"{clip_path.stem}-*.mp4")


def lock_for_clip(clip_path: Path) -> threading.RLock:
    normalized_path = clip_path.resolve()
    with _CLIP_LOCKS_GUARD:
        return _CLIP_LOCKS.setdefault(normalized_path, threading.RLock())


def public_media_path(path: Path) -> str:
    return path.resolve().relative_to(APP_DIRECTORY).as_posix()


def delete_generated_clip(file_path: str) -> None:
    clip_path = resolve_generated_clip(file_path)
    gif_path = gif_path_for_clip(clip_path)
    thumbnail_path = thumbnail_path_for_clip(clip_path)
    metadata_path = metadata_path_for_clip(clip_path)
    legacy_metadata_path = legacy_metadata_path_for_clip(clip_path)
    with lock_for_clip(clip_path):
        if not clip_path.is_file():
            raise MediaFileError("The selected generated clip no longer exists.", 404)
        for companion_path in (gif_path, thumbnail_path, metadata_path, legacy_metadata_path, *preview_paths_for_clip(clip_path)):
            try:
                companion_path.unlink(missing_ok=True)
            except OSError:
                pass
        clip_path.unlink()
