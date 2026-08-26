from pathlib import Path


APP_DIRECTORY = Path(__file__).resolve().parent
MEDIA_DIRECTORY = (APP_DIRECTORY / "static" / "media").resolve()
VIDEO_DIRECTORY = (MEDIA_DIRECTORY / "videos").resolve()
GIF_DIRECTORY = (MEDIA_DIRECTORY / "gifs").resolve()


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


def public_media_path(path: Path) -> str:
    return path.resolve().relative_to(APP_DIRECTORY).as_posix()


def delete_generated_clip(file_path: str) -> None:
    clip_path = resolve_generated_clip(file_path)
    gif_path = gif_path_for_clip(clip_path)
    gif_path.unlink(missing_ok=True)
    clip_path.unlink()
