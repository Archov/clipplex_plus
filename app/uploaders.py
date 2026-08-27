from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

from app.media_files import MediaFileError, VIDEO_DIRECTORY, resolve_generated_clip


APP_DIRECTORY = Path(__file__).resolve().parent
CLIP_DIRECTORY = VIDEO_DIRECTORY
REQUEST_TIMEOUT = (15, 300)


class UploadError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _setting(name: str) -> str:
    from app import settings
    return settings.get(name).strip()


def configured_uploaders() -> list[dict]:
    uploaders = []
    if _setting("streamable_login") and _setting("streamable_password"):
        uploaders.append({
            "id": "streamable",
            "label": "Streamable",
            "supports_tags": False,
            "supports_albums": False,
        })
    if _setting("immich_url") and _setting("immich_api_key"):
        uploaders.append({
            "id": "immich",
            "label": "Immich",
            "supports_tags": True,
            "supports_albums": True,
            "default_tag": _setting("immich_default_tag") or None,
        })
    return uploaders


def configured_uploader_ids() -> set[str]:
    return {uploader["id"] for uploader in configured_uploaders()}


def immich_asset_url(asset_id: str) -> str:
    """Return a browser URL without requiring upload credentials."""
    configured_url = _setting("immich_url").rstrip("/")
    if not asset_id or not configured_url:
        return ""
    web_url = configured_url[:-4].rstrip("/") if configured_url.lower().endswith("/api") else configured_url
    return f"{web_url}/photos/{asset_id}"


def resolve_clip_path(file_path: str) -> Path:
    try:
        return resolve_generated_clip(file_path)
    except MediaFileError as error:
        message = error.message.replace("may be used", "may be uploaded")
        if not isinstance(file_path, str) or not file_path.strip():
            message = "Select a generated clip to upload."
        raise UploadError(message, error.status_code) from error


def _deduplicate(values: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for raw_value in values:
        value = str(raw_value).strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _response_message(response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, list):
                message = "; ".join(str(item) for item in message)
            if message:
                return str(message)
    except ValueError:
        pass
    return (response.text or "").strip()[:300] or f"HTTP {response.status_code}"


class ImmichUploader:
    def __init__(self):
        configured_url = _setting("immich_url").rstrip("/")
        self.api_key = _setting("immich_api_key")
        if not configured_url or not self.api_key:
            raise UploadError("Immich is not configured.", 400)
        if configured_url.lower().endswith("/api"):
            self.api_url = configured_url
            self.web_url = configured_url[:-4].rstrip("/")
        else:
            self.api_url = f"{configured_url}/api"
            self.web_url = configured_url
        self.default_tag = _setting("immich_default_tag")

    @property
    def headers(self) -> dict:
        return {"Accept": "application/json", "x-api-key": self.api_key}

    def _request(self, method: str, path: str, **kwargs):
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}))
        try:
            response = requests.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                timeout=kwargs.pop("timeout", REQUEST_TIMEOUT),
                **kwargs,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            reason = _response_message(response) if response is not None else str(error)
            raise UploadError(f"Immich request failed: {reason}") from error
        except ValueError as error:
            raise UploadError("Immich returned an invalid response.") from error

    def get_tags(self) -> list[dict]:
        payload = self._request("GET", "/tags") or []
        return sorted(
            [
                {"id": item["id"], "name": item.get("value") or item.get("name") or "", "parent_id": item.get("parentId")}
                for item in payload
                if isinstance(item, dict) and item.get("id")
            ],
            key=lambda item: item["name"].casefold(),
        )

    def get_albums(self) -> list[dict]:
        payload = self._request("GET", "/albums") or []
        return sorted(
            [
                {"id": item["id"], "name": item.get("albumName") or "Untitled album"}
                for item in payload
                if isinstance(item, dict) and item.get("id")
            ],
            key=lambda item: item["name"].casefold(),
        )

    def options(self) -> dict:
        return {
            "tags": self.get_tags(),
            "albums": self.get_albums(),
            "default_tag": self.default_tag or None,
        }

    def _upload_asset(self, clip_path: Path) -> dict:
        file_stat = clip_path.stat()
        created = datetime.fromtimestamp(file_stat.st_ctime, timezone.utc).isoformat()
        modified = datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat()
        with clip_path.open("rb") as upload_file:
            payload = self._request(
                "POST",
                "/assets",
                data={
                    "fileCreatedAt": created,
                    "fileModifiedAt": modified,
                    "filename": clip_path.name,
                },
                files={"assetData": (clip_path.name, upload_file, "video/mp4")},
            )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise UploadError("Immich did not return an asset ID for the uploaded clip.")
        return payload

    def asset_url(self, asset_id: str) -> str:
        return immich_asset_url(asset_id)

    def _update_description(self, asset_id: str, title: str) -> None:
        self._request("PUT", f"/assets/{asset_id}", json={"description": title})

    def delete_asset(self, asset_id: str) -> None:
        if asset_id:
            self._request("DELETE", "/assets", json={"ids": [asset_id], "force": True})

    def _automatic_tag_parts(self, metadata: dict) -> list[str]:
        parts = []
        if _setting("immich_auto_tag_library") == "true" and metadata.get("media_library"):
            parts.append(str(metadata["media_library"]).strip())
        if _setting("immich_auto_tag_title") == "true":
            value = metadata.get("show") if metadata.get("media_type") == "episode" else metadata.get("title")
            if value:
                parts.append(str(value).strip())
        if _setting("immich_auto_tag_episode") == "true" and metadata.get("media_type") == "episode":
            season, episode = str(metadata.get("season_number") or ""), str(metadata.get("episode_number") or "")
            if season.isdigit() and episode.isdigit():
                parts.append(f"S{int(season):02d}E{int(episode):02d}")
        parts = _deduplicate(parts)
        return parts

    def _hierarchy_tag_ids(self, metadata: dict) -> list[str]:
        parts = self._automatic_tag_parts(metadata)
        if not parts:
            return []
        tags, parent_id = self.get_tags(), None
        for part in parts:
            matching = next((tag for tag in tags if tag.get("parent_id") == parent_id and tag["name"].rsplit("/", 1)[-1] == part), None)
            if matching is None:
                created = self._request("POST", "/tags", json={"name": part, **({"parentId": parent_id} if parent_id else {})})
                if not isinstance(created, dict) or not created.get("id"):
                    raise UploadError(f"Immich did not create the tag {part}.")
                matching = {"id": created["id"], "name": created.get("value") or part, "parent_id": parent_id}
                tags.append(matching)
            parent_id = matching["id"]
        return [parent_id]

    def _assign_tags(self, asset_id: str, tag_ids: list[str], tag_names: list[str]):
        requested_names = _deduplicate([*tag_names, self.default_tag])
        all_tags = self.get_tags() if requested_names else []
        tags_by_name = {tag["name"]: tag["id"] for tag in all_tags}
        missing_names = [name for name in requested_names if name not in tags_by_name]
        if missing_names:
            created_tags = self._request("PUT", "/tags", json={"tags": missing_names}) or []
            for tag in created_tags:
                if isinstance(tag, dict) and tag.get("id"):
                    tags_by_name[tag.get("value") or tag.get("name") or ""] = tag["id"]
        resolved_ids = _deduplicate([*tag_ids, *(tags_by_name.get(name, "") for name in requested_names)])
        unresolved = [name for name in requested_names if name not in tags_by_name]
        if unresolved:
            raise UploadError("Immich did not create these tags: " + ", ".join(unresolved))
        if resolved_ids:
            self._request(
                "PUT",
                "/tags/assets",
                json={"tagIds": resolved_ids, "assetIds": [asset_id]},
            )

    def _assign_existing_album(self, asset_id: str, album_id: str):
        self._request("PUT", f"/albums/{album_id}/assets", json={"ids": [asset_id]})

    def _create_album(self, asset_id: str, album_name: str):
        self._request("POST", "/albums", json={"albumName": album_name, "assetIds": [asset_id]})

    def upload(
        self,
        clip_path: Path,
        tag_ids: list[str],
        tag_names: list[str],
        album_ids: list[str],
        new_album_name: str,
        clip_title: str = "",
        auto_metadata: dict = None,
    ) -> tuple[dict, int]:
        uploaded = self._upload_asset(clip_path)
        asset_id = uploaded["id"]
        failures = []
        if clip_title:
            try:
                self._update_description(asset_id, clip_title)
            except UploadError as error:
                failures.append({"step": "description", "message": error.message})
        try:
            auto_metadata = auto_metadata or {}
            nested_auto_tags = self._hierarchy_tag_ids(auto_metadata) if auto_metadata else []
            self._assign_tags(asset_id, [*tag_ids, *nested_auto_tags], tag_names)
        except UploadError as error:
            failures.append({"step": "tags", "message": error.message})
        for album_id in _deduplicate(album_ids):
            try:
                self._assign_existing_album(asset_id, album_id)
            except UploadError as error:
                failures.append({"step": "album", "id": album_id, "message": error.message})
        if new_album_name.strip():
            try:
                self._create_album(asset_id, new_album_name.strip())
            except UploadError as error:
                failures.append({"step": "new_album", "message": error.message})

        result = {
            "result": "partial_success" if failures else "success",
            "uploader": "immich",
            "asset_id": asset_id,
            "upload_status": uploaded.get("status") or "created",
            "url": self.asset_url(asset_id),
            "failures": failures,
        }
        return result, 207 if failures else 200


def streamable_upload(clip_path: Path) -> tuple[dict, int]:
    email = _setting("streamable_login")
    password = _setting("streamable_password")
    if not email or not password:
        raise UploadError("Streamable is not configured.", 400)
    try:
        with clip_path.open("rb") as upload_file:
            response = requests.post(
                "https://api.streamable.com/upload",
                auth=(email, password),
                files={"file": (clip_path.name, upload_file, "video/mp4")},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        reason = _response_message(response) if response is not None else str(error)
        raise UploadError(f"Streamable upload failed: {reason}") from error
    except ValueError as error:
        raise UploadError("Streamable returned an invalid response.") from error
    shortcode = payload.get("shortcode") if isinstance(payload, dict) else None
    if not shortcode:
        raise UploadError("Streamable did not return a link for the uploaded clip.")
    return {
        "result": "success",
        "uploader": "streamable",
        "shortcode": shortcode,
        "url": f"https://streamable.com/{shortcode}",
        "failures": [],
    }, 200


def upload_clip(
    file_path: str,
    uploader: str,
    tag_ids: list[str] = None,
    tag_names: list[str] = None,
    album_ids: list[str] = None,
    new_album_name: str = "",
    apply_auto_tags: bool = False,
) -> tuple[dict, int]:
    if uploader not in configured_uploader_ids():
        raise UploadError("The selected upload service is not configured.", 400)
    clip_path = resolve_clip_path(file_path)
    if uploader == "streamable":
        return streamable_upload(clip_path)
    from app import clip_library
    metadata = clip_library.describe_clip(file_path)
    return ImmichUploader().upload(
        clip_path,
        tag_ids or [],
        tag_names or [],
        album_ids or [],
        new_album_name or "",
        metadata.get("clip_title") or metadata.get("display_heading") or "",
        metadata if apply_auto_tags else None,
    )
