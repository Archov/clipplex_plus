from dataclasses import dataclass
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

import ffmpeg
import requests


MEDIA_STATIC_PATH = "app/static/media"
GRAPHICAL_SUBTITLE_CODECS = {
    # FFmpeg codec names plus the names Plex commonly returns for the same codecs.
    "dvb_subtitle", "dvbsub", "dvb_teletext", "teletext", "dvd_subtitle",
    "dvdsub", "hdmv_pgs_subtitle", "pgs", "pgssub", "vobsub", "xsub",
}
GRAPHICAL_SUBTITLE_PROBE_WINDOW = 120
GRAPHICAL_SUBTITLE_CLUSTER_GAP = 2.0
TEXT_SUBTITLE_PREROLL = 30.0
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
    "slow", "slower", "veryslow", "placebo",
}
LOGGER = logging.getLogger(__name__)


def make_temporary_directory(prefix: str) -> str:
    """Create an FFmpeg-accessible temp directory, including on Python 3.13/Windows."""
    if os.name != "nt":
        return tempfile.mkdtemp(prefix=prefix)
    # Python 3.13 applies a restrictive Windows ACL for mkdir(mode=0o700), which
    # tempfile.mkdtemp uses. In some service/sandbox identities, child FFmpeg
    # processes cannot enter that directory. os.mkdir's default mode avoids it.
    for _ in range(100):
        path = os.path.join(os.getcwd(), f"{prefix}{secrets.token_hex(8)}")
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            continue
    raise FileExistsError("Could not allocate a temporary subtitle directory.")


def _is_selected(element) -> bool:
    return element is not None and element.attrib.get("selected") in ("1", "true", "True")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


FFMPEG_PROGRESS_KEYS = {
    "bitrate", "drop_frames", "dup_frames", "fps", "frame", "out_time",
    "out_time_ms", "out_time_us", "progress", "speed", "stream_0_0_q", "total_size",
}


def run_ffmpeg_with_progress(graph, duration_seconds, progress_callback=None):
    """Run an ffmpeg-python graph and report monotonic output-time percentages."""
    compiled = ffmpeg.compile(graph)
    command = [
        compiled[0], "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        *compiled[1:],
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    error_lines = []
    latest_percent = 0.0
    for raw_line in process.stdout or []:
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition("=")
        if separator and key in {"out_time_us", "out_time_ms"}:
            try:
                output_seconds = float(value) / 1_000_000.0
                percent = min(100.0, max(0.0, output_seconds / float(duration_seconds) * 100.0))
                latest_percent = max(latest_percent, percent)
                if progress_callback is not None:
                    progress_callback(latest_percent)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        elif not separator or (key not in FFMPEG_PROGRESS_KEYS and not key.startswith("stream_")):
            error_lines.append(line)
    return_code = process.wait()
    if return_code != 0:
        error_text = "\n".join(error_lines) or f"FFmpeg exited with status {return_code}."
        raise ffmpeg.Error(" ".join(command), b"", error_text.encode("utf-8", errors="replace"))
    if progress_callback is not None:
        progress_callback(100.0)


@dataclass
class MediaTrack:
    id: str
    index: int
    track_type: str
    codec: str = ""
    language: str = ""
    title: str = ""
    key: str = ""
    selected: bool = False
    available: bool = True
    unavailable_reason: str = ""
    subtitle_index: int = None
    probe_codec: str = ""

    @property
    def external(self) -> bool:
        return bool(self.key)

    @property
    def graphical(self) -> bool:
        """Use FFprobe's codec when available; Plex and FFmpeg use different PGS names."""
        return self.probe_codec in GRAPHICAL_SUBTITLE_CODECS or self.codec in GRAPHICAL_SUBTITLE_CODECS

    @property
    def label(self) -> str:
        details = [value for value in (self.language, self.title, self.codec.upper()) if value]
        return " · ".join(dict.fromkeys(details)) or f"{self.track_type.title()} track {self.id}"

    def as_option(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "selected": self.selected,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


class TrackSelectionError(Exception):
    def __init__(self, message: str, plex_data, failed_track: MediaTrack = None):
        super().__init__(message)
        self.message = message
        self.plex_data = plex_data
        self.failed_track = failed_track


class StaleSessionError(Exception):
    pass


class UnsupportedVideoError(Exception):
    """The selected source cannot be converted safely with the supported pipeline."""


class VideoConversionError(Exception):
    """FFmpeg failed specifically while converting the video image."""


@dataclass(frozen=True)
class VideoColorInfo:
    transfer: str = ""
    primaries: str = ""
    matrix: str = ""
    color_range: str = ""
    dolby_vision: bool = False
    dolby_profile: int = None
    dolby_compatibility_id: int = None

    @property
    def is_hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS


def _video_metadata_attributes(plex_data, video_index) -> dict:
    for part_name in ("metadata_part", "session_part"):
        part = getattr(plex_data, part_name, None)
        if part is None:
            continue
        video_streams = [stream for stream in part.findall("Stream") if stream.attrib.get("streamType") == "1"]
        for stream in video_streams:
            if _as_int(stream.attrib.get("index")) == video_index:
                return stream.attrib
        if len(video_streams) == 1:
            return video_streams[0].attrib
    return {}


def classify_video_color(plex_data) -> VideoColorInfo:
    video_index = getattr(plex_data, "video_index", None)
    streams = getattr(plex_data, "probe", {}).get("streams", [])
    stream = next(
        (item for item in streams if item.get("codec_type") == "video" and item.get("index") == video_index),
        next((item for item in streams if item.get("codec_type") == "video"), {}),
    )
    plex_attributes = _video_metadata_attributes(plex_data, video_index)
    plex_lower = {str(key).lower(): value for key, value in plex_attributes.items()}

    transfer = str(stream.get("color_transfer") or plex_lower.get("colortrc") or "").lower()
    transfer = {
        "smpte_st_2084": "smpte2084",
        "arib_std_b67": "arib-std-b67",
    }.get(transfer, transfer)
    primaries = str(stream.get("color_primaries") or plex_lower.get("colorprimaries") or "").lower()
    matrix = str(stream.get("color_space") or plex_lower.get("colorspace") or "").lower()
    color_range = str(stream.get("color_range") or plex_lower.get("colorrange") or "").lower()

    dolby_side_data = next(
        (
            item for item in stream.get("side_data_list", [])
            if "dovi" in str(item.get("side_data_type", "")).lower()
            or "dolby vision" in str(item.get("side_data_type", "")).lower()
        ),
        {},
    )
    dolby_profile = _as_int(
        dolby_side_data.get("dv_profile")
        or plex_lower.get("doviprofile")
        or plex_lower.get("dolbyvisionprofile")
    )
    dolby_compatibility_id = _as_int(
        dolby_side_data.get("dv_bl_signal_compatibility_id")
        or dolby_side_data.get("bl_signal_compatibility_id")
        or plex_lower.get("doviblcompatid")
        or plex_lower.get("dolbyvisionblcompatid")
    )
    plex_description = " ".join(str(value).lower() for value in plex_attributes.values())
    codec_tag = str(stream.get("codec_tag_string") or "").lower()
    dolby_vision = bool(
        dolby_side_data
        or codec_tag.startswith(("dvhe", "dvh1", "dvav", "dva1"))
        or "dolby vision" in plex_description
        or "dovi" in plex_description
    )

    if dolby_vision:
        if dolby_profile == 5:
            raise UnsupportedVideoError(
                "Dolby Vision Profile 5 cannot be converted safely without metadata-aware Dolby Vision processing."
            )
        compatible_base = dolby_compatibility_id not in (None, 0) or dolby_profile == 7
        if not compatible_base or transfer not in HDR_TRANSFERS:
            raise UnsupportedVideoError(
                "This Dolby Vision source does not expose a confirmed HDR-compatible base layer."
            )

    return VideoColorInfo(
        transfer=transfer,
        primaries=primaries,
        matrix=matrix,
        color_range=color_range,
        dolby_vision=dolby_vision,
        dolby_profile=dolby_profile,
        dolby_compatibility_id=dolby_compatibility_id,
    )


class PlexSessions:
    """Lightweight access to Plex playback sessions without probing media files."""

    def __init__(self):
        self.plex_url = (os.environ.get("PLEX_URL") or "").rstrip("/")
        self.plex_token = os.environ.get("PLEX_TOKEN") or ""
        self.headers = {"X-Plex-Token": self.plex_token}

    def request_xml(self, path: str) -> ET.Element:
        response = requests.get(f"{self.plex_url}{path}", headers=self.headers, timeout=30)
        response.raise_for_status()
        return ET.fromstring(response.content)

    def fetch(self) -> ET.Element:
        return self.request_xml("/status/sessions")

    @staticmethod
    def session_identifier(element: ET.Element) -> str:
        session = element.find("Session")
        if session is not None and session.attrib.get("id"):
            return session.attrib["id"]
        if element.attrib.get("sessionKey"):
            return element.attrib["sessionKey"]
        player = element.find("Player")
        player_id = ""
        if player is not None:
            player_id = (
                player.attrib.get("machineIdentifier")
                or player.attrib.get("clientIdentifier")
                or player.attrib.get("title")
                or ""
            )
        user = element.find("User")
        username = user.attrib.get("title", "") if user is not None else ""
        if player_id or username:
            return f"fallback:{player_id or 'unknown-player'}:{username or 'unknown-user'}"
        media_identity = element.attrib.get("ratingKey") or element.attrib.get("key") or "unknown"
        return f"fallback:unknown-player:unknown-user:{media_identity}"

    @staticmethod
    def media_identity(element: ET.Element) -> str:
        identity = [element.attrib.get("ratingKey") or element.attrib.get("key") or ""]
        media_elements = element.findall("Media")
        media = next((item for item in media_elements if _is_selected(item)), None)
        if media is None and len(media_elements) == 1:
            media = media_elements[0]
        if media is not None:
            identity.append(media.attrib.get("id") or "")
            parts = media.findall("Part")
            part = next((item for item in parts if _is_selected(item)), None)
            if part is None and len(parts) == 1:
                part = parts[0]
            if part is not None:
                identity.append(part.attrib.get("id") or "")
        return ":".join(value for value in identity if value)

    @classmethod
    def summary(cls, element: ET.Element) -> dict:
        user = element.find("User")
        player = element.find("Player")
        media_type = element.attrib.get("type", "")
        title = element.attrib.get("title") or element.attrib.get("originalTitle") or "Untitled video"
        if media_type == "episode":
            show = element.attrib.get("grandparentTitle") or ""
            title = f"{show} - {title}".strip(" -")
        player_name = "Unknown player"
        player_product = ""
        state = element.attrib.get("state") or "unknown"
        if player is not None:
            player_name = player.attrib.get("title") or player.attrib.get("device") or player_name
            player_product = player.attrib.get("product") or player.attrib.get("platform") or ""
            state = player.attrib.get("state") or state
        return {
            "session_id": cls.session_identifier(element),
            "media_identity": cls.media_identity(element),
            "username": user.attrib.get("title", "Unknown user") if user is not None else "Unknown user",
            "player_name": player_name,
            "player_product": player_product,
            "state": state,
            "title": title,
            "media_type": media_type,
            "view_offset_ms": _as_int(element.attrib.get("viewOffset")) or 0,
            "duration_ms": _as_int(element.attrib.get("duration")) or 0,
        }

    def list_video_sessions(self, sessions_xml: ET.Element = None) -> list:
        sessions_xml = sessions_xml if sessions_xml is not None else self.fetch()
        return [
            self.summary(element)
            for element in list(sessions_xml)
            if element.tag == "Video" and element.findall("Media")
        ]


class PlexInfo:
    def __init__(self, username=None, session_id=None, sessions_xml=None, inspect_media=True):
        self.session_client = PlexSessions()
        self.plex_url = self.session_client.plex_url
        self.plex_token = self.session_client.plex_token
        self.headers = self.session_client.headers
        self.sessions_xml = sessions_xml if sessions_xml is not None else self.session_client.fetch()
        self.session_element = self._get_session(username=username, session_id=session_id)
        user = self.session_element.find("User")
        self.username = user.attrib.get("title", "Unknown user") if user is not None else (username or "Unknown user")
        self.session_id = list(self.sessions_xml).index(self.session_element)
        self.session_identifier = self._get_session_identifier()
        self.media_key = self._get_media_key()
        self.media_identity = PlexSessions.media_identity(self.session_element) or self.media_key
        self.media_path_xml = self._request_xml(self.media_key)
        self.metadata_element = self._get_metadata_element()
        self.session_media = self._active_element(self.session_element.findall("Media"), "media version")
        self.session_part = self._active_element(self.session_media.findall("Part"), "media part")
        self.metadata_part = self._get_metadata_part()
        self.media_path = self._get_file_path()
        self.media_fps = self._get_media_fps()
        self.media_type = self.metadata_element.attrib.get("type", self.session_element.attrib.get("type", ""))
        self.media_title = self._get_file_title()
        self.current_media_time_int = int(self.session_element.attrib.get("viewOffset", 0))
        self.current_media_time_str = Utils(offset=self.current_media_time_int).offset_to_time
        self.duration_ms = _as_int(self.session_element.attrib.get("duration")) or 0
        self.probe = {}
        self.video_index = None
        self.audio_tracks, self.subtitle_tracks = [], []
        if inspect_media:
            self.probe = self._probe_media()
            self.video_index = self._get_video_index()
            self.audio_tracks, self.subtitle_tracks = self._get_tracks()
            self._validate_track_availability()

    def _request_xml(self, path: str) -> ET.Element:
        return self.session_client.request_xml(path)

    def _get_session(self, username=None, session_id=None) -> ET.Element:
        for session in list(self.sessions_xml):
            if session.tag != "Video":
                continue
            if session_id and PlexSessions.session_identifier(session) == str(session_id):
                return session
            user = session.find("User")
            if not session_id and username and user is not None and user.attrib.get("title") == username:
                return session
        if session_id:
            raise StaleSessionError("The selected Plex playback session is no longer active.")
        raise Exception(f"No stream running for user {username}")

    def _get_session_identifier(self) -> str:
        return PlexSessions.session_identifier(self.session_element)

    def preview_image(self, at_ms: int, timeout=10):
        if at_ms < 0 or (self.duration_ms and at_ms > self.duration_ms):
            raise ValueError("The preview timestamp is outside the playing video.")
        media = ffmpeg.input(self.media_path, ss=at_ms / 1000.0)
        frame = media.video.filter("scale", 480, -2)
        graph = ffmpeg.output(
            frame,
            "pipe:",
            format="image2",
            vframes=1,
            vcodec="mjpeg",
            **{"q:v": 3},
        ).global_args("-hide_banner", "-loglevel", "error")
        try:
            completed = subprocess.run(
                ffmpeg.compile(graph),
                check=True,
                capture_output=True,
                timeout=timeout,
            )
            if completed.stdout:
                return completed.stdout, "image/jpeg"
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            LOGGER.warning("Current-frame preview failed", exc_info=True)

        thumb = self.metadata_element.attrib.get("thumb") or self.session_element.attrib.get("thumb")
        if thumb:
            response = requests.get(f"{self.plex_url}{thumb}", headers=self.headers, timeout=5)
            response.raise_for_status()
            return response.content, response.headers.get("Content-Type", "image/jpeg")
        raise FileNotFoundError("No preview is available for this session.")

    def _get_media_key(self) -> str:
        key = self.session_element.attrib.get("key")
        if not key:
            raise Exception("The playing Plex session has no media key")
        return key

    def _get_metadata_element(self) -> ET.Element:
        metadata = next(iter(self.media_path_xml), None)
        if metadata is None:
            raise Exception("Plex returned no metadata for the playing video")
        return metadata

    @staticmethod
    def _active_element(elements, label: str) -> ET.Element:
        if not elements:
            raise Exception(f"The playing Plex session has no {label}")
        selected = [element for element in elements if _is_selected(element)]
        if len(selected) == 1:
            return selected[0]
        if len(elements) == 1:
            return elements[0]
        raise Exception(f"Plex did not identify the active {label}")

    def _get_metadata_part(self) -> ET.Element:
        part_id = self.session_part.attrib.get("id")
        metadata_parts = self.metadata_element.findall("./Media/Part")
        if part_id:
            for part in metadata_parts:
                if part.attrib.get("id") == part_id:
                    return part
        if len(metadata_parts) == 1:
            return metadata_parts[0]
        raise Exception("The active Plex media part could not be matched to its library metadata")

    def _get_file_path(self) -> str:
        file_path = self.metadata_part.attrib.get("file") or self.session_part.attrib.get("file")
        if not file_path:
            raise Exception("The active Plex media part has no local file path")
        return file_path

    def _get_media_fps(self) -> float:
        for source in (self.metadata_part, self.session_part):
            for stream in source.findall("Stream"):
                if stream.attrib.get("streamType") == "1" and stream.attrib.get("frameRate"):
                    return float(stream.attrib["frameRate"])
        return 0.0

    def _get_file_title(self) -> str:
        attributes = self.metadata_element.attrib
        if self.media_type == "episode":
            return f"{attributes.get('grandparentTitle', '')} - {attributes.get('title', '')}".strip(" -")
        return attributes.get("title", "")

    def _probe_media(self) -> dict:
        try:
            return ffmpeg.probe(self.media_path)
        except ffmpeg.Error as error:
            raise Exception("FFmpeg could not inspect the active media file") from error

    def _get_video_index(self) -> int:
        video_streams = [stream for stream in self.probe.get("streams", []) if stream.get("codec_type") == "video"]
        session_video = next(
            (stream for stream in self.session_part.findall("Stream") if stream.attrib.get("streamType") == "1"),
            None,
        )
        selected_index = _as_int(session_video.attrib.get("index")) if session_video is not None else None
        if selected_index is not None and any(stream.get("index") == selected_index for stream in video_streams):
            return selected_index
        if video_streams:
            return int(video_streams[0]["index"])
        raise Exception("The active media file has no video stream")

    @staticmethod
    def _track_from_element(element: ET.Element, track_type: str, selected: bool) -> MediaTrack:
        attributes = element.attrib
        return MediaTrack(
            id=str(attributes.get("id", "")),
            index=_as_int(attributes.get("index")),
            track_type=track_type,
            codec=(attributes.get("codec") or attributes.get("format") or "").lower(),
            language=attributes.get("language") or attributes.get("languageCode") or "",
            title=attributes.get("extendedDisplayTitle") or attributes.get("displayTitle") or attributes.get("title") or "",
            key=attributes.get("key") or "",
            selected=selected,
        )

    def _get_tracks(self):
        selected_ids = {
            stream_type: {
                stream.attrib.get("id")
                for stream in self.session_part.findall("Stream")
                if stream.attrib.get("streamType") == stream_type and _is_selected(stream)
            }
            for stream_type in ("2", "3")
        }
        metadata_streams = self.metadata_part.findall("Stream")
        audio_tracks = [
            self._track_from_element(stream, "audio", stream.attrib.get("id") in selected_ids["2"])
            for stream in metadata_streams if stream.attrib.get("streamType") == "2"
        ]
        subtitle_tracks = [
            self._track_from_element(stream, "subtitle", stream.attrib.get("id") in selected_ids["3"])
            for stream in metadata_streams if stream.attrib.get("streamType") == "3"
        ]
        known_ids = {track.id for track in audio_tracks + subtitle_tracks}
        for stream in self.session_part.findall("Stream"):
            stream_type = stream.attrib.get("streamType")
            if stream_type not in ("2", "3") or stream.attrib.get("id") in known_ids:
                continue
            track_type = "audio" if stream_type == "2" else "subtitle"
            track = self._track_from_element(stream, track_type, _is_selected(stream))
            (audio_tracks if track_type == "audio" else subtitle_tracks).append(track)
        return audio_tracks, subtitle_tracks

    def _validate_track_availability(self):
        probe_streams = self.probe.get("streams", [])
        probe_by_index = {stream.get("index"): stream for stream in probe_streams}
        subtitle_indexes = [stream.get("index") for stream in probe_streams if stream.get("codec_type") == "subtitle"]
        for track in self.audio_tracks:
            stream = probe_by_index.get(track.index)
            if track.index is None or stream is None or stream.get("codec_type") != "audio":
                track.available = False
                track.unavailable_reason = "This audio stream is not present in the mounted media file."
        for track in self.subtitle_tracks:
            if track.external:
                if not track.key.startswith("/"):
                    track.available = False
                    track.unavailable_reason = "Plex did not provide a usable subtitle download key."
                elif track.graphical:
                    track.available = False
                    track.unavailable_reason = "External bitmap subtitle files cannot be burned without their companion assets."
                continue
            stream = probe_by_index.get(track.index)
            if track.index is None or stream is None or stream.get("codec_type") != "subtitle":
                track.available = False
                track.unavailable_reason = "This subtitle stream is not present in the mounted media file."
                continue
            track.probe_codec = (stream.get("codec_name") or "").lower()
            track.subtitle_index = subtitle_indexes.index(track.index)

    def _selected_track(self, tracks, label: str):
        selected = [track for track in tracks if track.selected]
        if len(selected) != 1:
            raise TrackSelectionError(f"Plex did not report one selected {label} track.", self)
        return selected[0]

    @staticmethod
    def _track_by_id(tracks, track_id: str, label: str):
        for track in tracks:
            if track.id == str(track_id):
                return track
        raise ValueError(f"The requested {label} track is not available for this media part.")

    def resolve_tracks(self, audio_stream_id=None, subtitle_stream_id=None):
        try:
            audio_track = self._track_by_id(self.audio_tracks, audio_stream_id, "audio") if audio_stream_id is not None else self._selected_track(self.audio_tracks, "audio")
            if subtitle_stream_id == "none":
                subtitle_track = None
            elif subtitle_stream_id is not None:
                subtitle_track = self._track_by_id(self.subtitle_tracks, subtitle_stream_id, "subtitle")
            else:
                selected_subtitles = [track for track in self.subtitle_tracks if track.selected]
                if len(selected_subtitles) > 1:
                    raise TrackSelectionError("Plex reported more than one selected subtitle track.", self)
                subtitle_track = selected_subtitles[0] if selected_subtitles else None
        except ValueError as error:
            raise TrackSelectionError(str(error), self) from error
        for track in (audio_track, subtitle_track):
            if track is not None and not track.available:
                raise TrackSelectionError(track.unavailable_reason, self, track)
        return audio_track, subtitle_track

    def track_options(self) -> dict:
        subtitle_off = {
            "id": "none", "label": "Off",
            "selected": not any(track.selected for track in self.subtitle_tracks),
            "available": True, "unavailable_reason": "",
        }
        return {
            "audio": [track.as_option() for track in self.audio_tracks],
            "subtitles": [subtitle_off] + [track.as_option() for track in self.subtitle_tracks],
        }


class Snapshot:
    def __init__(self, media_path: str, time: str, fps: float):
        self.media_path = media_path
        self.time = time
        self.fps = int(fps)

    def _download_frames(self):
        output_pattern = os.path.join(
            MEDIA_STATIC_PATH,
            "images",
            f"{self.time.replace(':', '_')}_%03d.jpg",
        )
        command = [
            "ffmpeg",
            "-ss",
            str(self.time),
            "-i",
            self.media_path,
            "-vframes",
            str(self.fps),
            "-qscale:v",
            "2",
            output_pattern,
        ]
        subprocess.call(command, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


class Video:
    def __init__(self, plex_data: PlexInfo, time: str, duration, file_name: str, audio_track: MediaTrack, subtitle_track: MediaTrack = None):
        self.plex_data = plex_data
        self.media_path = plex_data.media_path
        attributes = plex_data.metadata_element.attrib
        self.metadata_title = attributes.get("title", "")
        self.metadata_current_media_time = plex_data.current_media_time_str
        self.metadata_username = plex_data.username
        if plex_data.media_type == "episode":
            self.metadata_season = attributes.get("parentIndex", "")
            self.metadata_episode_number = attributes.get("index", "")
            self.metadata_showname = attributes.get("grandparentTitle", "")
        else:
            self.metadata_season = self.metadata_episode_number = self.metadata_showname = ""
        if isinstance(time, str):
            self.start_ms = Utils.time_to_milliseconds(time)
        else:
            self.start_ms = int(time)
        self.start_seconds = self.start_ms / 1000.0
        self.time = self.start_seconds
        self.duration = float(duration)
        self.metadata_current_media_time = Utils.milliseconds_to_string(self.start_ms)
        self.file_name = file_name
        self.audio_track = audio_track
        self.subtitle_track = subtitle_track
        self.color_info = classify_video_color(plex_data)
        self.output_path = f"{MEDIA_STATIC_PATH}/videos/{self.file_name}.mp4"

    @property
    def x264_preset(self) -> str:
        preset = (os.environ.get("FFMPEG_PRESET") or "veryfast").lower()
        if preset not in X264_PRESETS:
            LOGGER.warning("Ignoring invalid FFMPEG_PRESET=%r; using veryfast", preset)
            return "veryfast"
        return preset

    def _graphical_subtitle_seek_start(self) -> float:
        """Seek before the active bitmap composition so a cue crossing clip start survives."""
        window_start = max(0.0, self.start_seconds - GRAPHICAL_SUBTITLE_PROBE_WINDOW)
        interval_duration = self.start_seconds - window_start + 1.0
        try:
            packet_probe = ffmpeg.probe(
                self.media_path,
                select_streams=str(self.subtitle_track.index),
                show_packets=None,
                show_entries="packet=pts_time",
                read_intervals=f"{window_start}%+{interval_duration}",
            )
            packet_times = sorted(
                float(packet["pts_time"])
                for packet in packet_probe.get("packets", [])
                if packet.get("pts_time") is not None and float(packet["pts_time"]) <= self.start_seconds
            )
        except (ffmpeg.Error, TypeError, ValueError, KeyError):
            LOGGER.warning(
                "Could not inspect bitmap subtitle packets for stream %s; using a 10 second preroll",
                self.subtitle_track.index,
                exc_info=True,
            )
            return max(0.0, self.start_seconds - 10.0)

        if not packet_times:
            return self.start_seconds

        # A PGS/DVB/VobSub display update can span several adjacent packets. Include
        # the entire latest packet cluster, plus a small decoder initialization pad.
        cluster_start = packet_times[-1]
        for packet_time in reversed(packet_times[:-1]):
            if cluster_start - packet_time > GRAPHICAL_SUBTITLE_CLUSTER_GAP:
                break
            cluster_start = packet_time
        return max(window_start, cluster_start - 0.5)

    def _download_external_subtitle(self, progress_callback=None) -> str:
        suffix = f".{self.subtitle_track.codec}" if self.subtitle_track.codec.isalnum() else ".sub"
        response = requests.get(
            f"{self.plex_data.plex_url}{self.subtitle_track.key}",
            headers=self.plex_data.headers,
            timeout=30,
            stream=True,
        )
        response.raise_for_status()
        temporary = tempfile.NamedTemporaryFile(
            prefix="clipplex-subtitle-",
            suffix=suffix,
            delete=False,
            dir=os.getcwd() if os.name == "nt" else None,
        )
        try:
            total_size = _as_int(response.headers.get("Content-Length")) or 0
            downloaded = 0
            chunks = response.iter_content(chunk_size=64 * 1024) if hasattr(response, "iter_content") else [response.content]
            for chunk in chunks:
                if not chunk:
                    continue
                temporary.write(chunk)
                downloaded += len(chunk)
                if progress_callback is not None and total_size:
                    progress_callback(min(100.0, downloaded / total_size * 100.0))
        except Exception:
            temporary.close()
            try:
                os.remove(temporary.name)
            except OSError:
                pass
            raise
        finally:
            temporary.close()
        if progress_callback is not None:
            progress_callback(100.0)
        return temporary.name

    def build_text_subtitle_extract_ffmpeg(self, output_path: str):
        """Include cues that begin before the clip but remain active at its start."""
        seek_start, preroll = self._text_subtitle_seek()
        media = ffmpeg.input(self.media_path, ss=seek_start, t=float(self.duration) + preroll)
        subtitle = media[str(self.subtitle_track.index)]
        return ffmpeg.output(subtitle, output_path, scodec="ass").overwrite_output()

    def _text_subtitle_seek(self):
        seek_start = max(0.0, self.start_seconds - TEXT_SUBTITLE_PREROLL)
        return seek_start, self.start_seconds - seek_start

    def _extract_embedded_text_subtitle(self, output_path: str, progress_callback=None):
        _, preroll = self._text_subtitle_seek()
        run_ffmpeg_with_progress(
            self.build_text_subtitle_extract_ffmpeg(output_path),
            float(self.duration) + preroll,
            progress_callback,
        )

    def _extract_embedded_fonts(self, destination: str, progress_callback=None):
        attachments = [
            stream for stream in self.plex_data.probe.get("streams", [])
            if stream.get("codec_type") == "attachment"
        ]
        for attachment_number, attachment in enumerate(attachments):
            attachment_name = (attachment.get("tags") or {}).get("filename") or ""
            extension = os.path.splitext(os.path.basename(attachment_name))[1].lower()
            if extension not in {".otf", ".ttf", ".ttc", ".woff", ".woff2"}:
                codec_name = (attachment.get("codec_name") or "").lower()
                if codec_name in {"otf", "opentype"}:
                    extension = ".otf"
                elif codec_name in {"ttf", "truetype"}:
                    extension = ".ttf"
                else:
                    extension = ".font"
            output_path = os.path.join(destination, f"attachment-{attachment_number}{extension}")
            command = [
                "ffmpeg", "-v", "error",
                f"-dump_attachment:t:{attachment_number}", output_path,
                "-i", self.media_path,
                "-t", "0", "-f", "null", "-",
            ]
            try:
                subprocess.run(command, check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                LOGGER.warning(
                    "Could not extract embedded font attachment %s",
                    attachment_number,
                    exc_info=True,
                )
            if progress_callback is not None:
                progress_callback(attachment_number + 1, len(attachments))

    def _tone_map_to_sdr(self, video):
        if not self.color_info.is_hdr:
            return video
        input_primaries = self.color_info.primaries or "bt2020"
        input_matrix = self.color_info.matrix or "bt2020nc"
        input_range = {
            "limited": "tv",
            "full": "pc",
        }.get(self.color_info.color_range, self.color_info.color_range or "tv")
        video = video.filter(
            "zscale",
            pin=input_primaries,
            tin=self.color_info.transfer,
            min=input_matrix,
            rin=input_range,
            t="linear",
            npl=100,
        )
        video = video.filter("format", "gbrpf32le")
        video = video.filter("zscale", p="bt709")
        video = video.filter("tonemap", tonemap="mobius", param=0.3, desat=2)
        video = video.filter(
            "zscale",
            p="bt709",
            t="bt709",
            m="bt709",
            r="tv",
            d="error_diffusion",
        )
        return video.filter("format", "yuv420p")

    @staticmethod
    def _scale_for_compatibility(video):
        """Fit inside 1920x1080 without upscaling, padding, or changing aspect ratio."""
        video = video.filter(
            "scale",
            "min(1920,iw)",
            "min(1080,ih)",
            force_original_aspect_ratio="decrease",
            force_divisible_by=2,
            flags="lanczos",
        )
        return video.filter("setsar", "1")

    @staticmethod
    def _tag_bt709_output(video):
        """Keep converted HDR frame metadata from leaking BT.2020 into x264."""
        return video.filter(
            "setparams",
            range="tv",
            color_primaries="bt709",
            color_trc="bt709",
            colorspace="bt709",
        )

    @staticmethod
    def _is_color_conversion_failure(stderr: str) -> bool:
        message = stderr.lower()
        return any(token in message for token in (
            "zscale", "tonemap", "colorspace", "colourspace",
            "no path between colorspaces", "no such filter",
        ))

    @property
    def render_message(self) -> str:
        if self.color_info.is_hdr:
            return "Tone-mapping HDR, burning subtitles, and rendering the clip."
        return "Burning subtitles and rendering the clip."

    def build_ffmpeg(self, subtitle_path=None, subtitle_fonts_dir=None):
        graphical_subtitle = self.subtitle_track is not None and self.subtitle_track.graphical
        seek_start = self.start_seconds
        preroll = 0.0
        if graphical_subtitle and not self.subtitle_track.external:
            seek_start = self._graphical_subtitle_seek_start()
            preroll = self.start_seconds - seek_start

        media = ffmpeg.input(self.media_path, ss=seek_start, t=float(self.duration) + preroll)
        video = self._tone_map_to_sdr(media[str(self.plex_data.video_index)])
        audio = media[str(self.audio_track.index)]
        if self.subtitle_track is not None:
            if not graphical_subtitle:
                video = self._scale_for_compatibility(video)
                source_path = subtitle_path or self.media_path
                if os.name == "nt":
                    source_path = os.path.relpath(source_path, os.getcwd()).replace("\\", "/")
                subtitle_options = {}
                if self.subtitle_track.external:
                    video = video.filter("setpts", f"PTS+{self.start_seconds}/TB")
                elif subtitle_path is not None:
                    _, text_preroll = self._text_subtitle_seek()
                    if text_preroll:
                        video = video.filter("setpts", f"PTS+{text_preroll}/TB")
                elif subtitle_path is None:
                    subtitle_options["si"] = self.subtitle_track.subtitle_index
                    video = video.filter("setpts", f"PTS+{self.start_seconds}/TB")
                if subtitle_fonts_dir:
                    if os.name == "nt":
                        subtitle_fonts_dir = os.path.relpath(subtitle_fonts_dir, os.getcwd()).replace("\\", "/")
                    subtitle_options["fontsdir"] = subtitle_fonts_dir
                video = video.filter("subtitles", source_path, **subtitle_options)
                video = video.filter("setpts", "PTS-STARTPTS")
            else:
                video = ffmpeg.overlay(video, media[str(self.subtitle_track.index)], eof_action="pass", repeatlast=0)
                if preroll:
                    video = video.filter("trim", start=preroll, duration=self.duration)
                video = self._scale_for_compatibility(video)
                video = video.filter("setpts", "PTS-STARTPTS")
        else:
            video = self._scale_for_compatibility(video)
            video = video.filter("setpts", "PTS-STARTPTS")
        if self.color_info.is_hdr:
            video = self._tag_bt709_output(video)
        if preroll:
            audio = audio.filter("atrim", start=preroll, duration=self.duration)
        audio = audio.filter("asetpts", "PTS-STARTPTS")
        metadata = {
            "metadata:g:0": f"title={self.metadata_title}",
            "metadata:g:1": f"season_number={self.metadata_season}",
            "metadata:g:2": f"show={self.metadata_showname}",
            "metadata:g:3": f"episode_id={self.metadata_episode_number}",
            "metadata:g:4": f"comment={self.metadata_current_media_time}",
            "metadata:g:5": f"artist={self.metadata_username}",
            "profile:a": "aac_low",
        }
        if self.color_info.is_hdr:
            metadata.update({
                "color_primaries": "bt709",
                "color_trc": "bt709",
                "colorspace": "bt709",
                "color_range": "tv",
                "x264-params": "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
            })
        return ffmpeg.output(
            video, audio, self.output_path, map_metadata=-1, vcodec="libx264", acodec="aac",
            audio_bitrate="192k", ac=2, ar=48000, pix_fmt="yuv420p", crf=18, preset=self.x264_preset,
            movflags="+faststart", **metadata,
        ).overwrite_output()

    def extract_video(self, progress_callback=None):
        subtitle_path = None
        subtitle_directory = None
        started_at = time.monotonic()
        encode_elapsed = None

        def emit(stage, overall, stage_progress, message):
            if progress_callback is not None:
                progress_callback(stage, overall, stage_progress, message)

        try:
            if self.subtitle_track is not None:
                subtitle_started_at = time.monotonic()
                if self.subtitle_track.external:
                    emit("preparing_subtitles", 10, 0, "Downloading the selected subtitle track.")
                    subtitle_path = self._download_external_subtitle(
                        lambda percent: emit(
                            "preparing_subtitles", 10 + percent * 0.25, percent,
                            "Downloading the selected subtitle track.",
                        )
                    )
                elif not self.subtitle_track.graphical:
                    emit("preparing_subtitles", 10, 0, "Extracting ASS subtitles with boundary preroll.")
                    subtitle_directory = make_temporary_directory("clipplex-subtitle-")
                    subtitle_path = os.path.join(subtitle_directory, "selected.ass")
                    self._extract_embedded_text_subtitle(
                        subtitle_path,
                        lambda percent: emit(
                            "preparing_subtitles", 10 + percent * 0.25, percent,
                            "Extracting ASS subtitles with boundary preroll.",
                        ),
                    )
                    emit("preparing_subtitles", 35, 0, "Extracting embedded subtitle fonts.")
                    self._extract_embedded_fonts(
                        subtitle_directory,
                        lambda completed, total: emit(
                            "preparing_subtitles",
                            35 + (completed / total * 5.0 if total else 5.0),
                            completed / total * 100.0 if total else 100.0,
                            f"Extracting embedded subtitle fonts ({completed}/{total}).",
                        ),
                    )
                else:
                    emit("preparing_subtitles", 15, 0, "Analyzing bitmap subtitle preroll.")
                subtitle_elapsed = time.monotonic() - subtitle_started_at
                log_subtitle = LOGGER.warning if subtitle_elapsed > 5.0 else LOGGER.info
                log_subtitle(
                    "Subtitle preparation finished in %.2fs (codec=%s)",
                    subtitle_elapsed,
                    self.subtitle_track.probe_codec or self.subtitle_track.codec or "unknown",
                )
            emit("rendering", 40, 0, self.render_message)
            encode_started_at = time.monotonic()
            run_ffmpeg_with_progress(
                self.build_ffmpeg(subtitle_path, subtitle_directory),
                float(self.duration),
                lambda percent: emit(
                    "rendering", 40 + percent * 0.55, percent,
                    self.render_message,
                ),
            )
            encode_elapsed = time.monotonic() - encode_started_at
            emit("finalizing", 97, 50, "Finalizing the MP4 for playback.")
        except (ffmpeg.Error, requests.RequestException, OSError) as error:
            if isinstance(error, ffmpeg.Error):
                stderr = (error.stderr or b"").decode("utf-8", errors="replace")
                LOGGER.error("FFmpeg clip processing failed:\n%s", stderr.rstrip())
            else:
                LOGGER.exception("Clip render failed")
            if os.path.exists(self.output_path):
                os.remove(self.output_path)
            if (
                self.color_info.is_hdr
                and isinstance(error, ffmpeg.Error)
                and self._is_color_conversion_failure(stderr)
            ):
                raise VideoConversionError(
                    "FFmpeg could not tone-map this HDR video to browser-compatible SDR."
                ) from error
            if self.subtitle_track is not None:
                self.subtitle_track.available = False
                self.subtitle_track.unavailable_reason = "FFmpeg could not load or burn this subtitle track."
                raise TrackSelectionError(self.subtitle_track.unavailable_reason, self.plex_data, self.subtitle_track) from error
            self.audio_track.available = False
            self.audio_track.unavailable_reason = "FFmpeg could not decode or encode this audio track."
            raise TrackSelectionError(
                self.audio_track.unavailable_reason,
                self.plex_data,
                self.audio_track,
            ) from error
        finally:
            subtitle_codec = "off"
            if self.subtitle_track is not None:
                subtitle_codec = self.subtitle_track.probe_codec or self.subtitle_track.codec or "unknown"
            elapsed = time.monotonic() - started_at
            log_render = LOGGER.warning if elapsed > max(30.0, float(self.duration) * 2.0) else LOGGER.info
            log_render(
                "Clip request work finished in %.2fs (encoding=%s, duration=%ss, preset=%s, subtitle=%s)",
                elapsed,
                f"{encode_elapsed:.2f}s" if encode_elapsed is not None else "failed",
                self.duration,
                self.x264_preset,
                subtitle_codec,
            )
            if subtitle_directory:
                shutil.rmtree(subtitle_directory, ignore_errors=True)
            elif subtitle_path and os.path.exists(subtitle_path):
                os.remove(subtitle_path)


class Utils:
    def __init__(self, offset: int = 0):
        self.offset_to_time = self.milli_to_string(offset)

    def milli_to_string(self, millisec: int) -> str:
        return self.milliseconds_to_string(millisec)

    @staticmethod
    def milliseconds_to_string(milliseconds: int) -> str:
        milliseconds = max(0, int(milliseconds))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"

    @staticmethod
    def time_to_milliseconds(time_value: str) -> int:
        match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?", str(time_value).strip())
        if not match:
            raise ValueError("Timestamps must use HH:MM:SS.mmm format.")
        hours, minutes, seconds = (int(match.group(index)) for index in range(1, 4))
        fraction = (match.group(4) or "0").ljust(3, "0")
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction)

    @staticmethod
    def time_to_seconds(time_value: str) -> float:
        return Utils.time_to_milliseconds(time_value) / 1000.0

    def add_time(self, current_time: str, time_to_add: int) -> str:
        milliseconds = self.time_to_milliseconds(current_time) + int(time_to_add) * 1000
        return self.milliseconds_to_string(milliseconds)

    def _pad_time(self, time) -> str:
        return f"0{time}" if len(str(time)) < 2 else time

    def calculate_clip_time(self, start, end) -> float:
        return (self.time_to_milliseconds(end) - self.time_to_milliseconds(start)) / 1000.0

    def get_images_in_folder() -> list:
        folder = os.path.join(MEDIA_STATIC_PATH, "images")
        return sorted(f"static/media/images/{name}" for name in os.listdir(folder))

    @staticmethod
    def get_video_in_folder(file_path) -> dict:
        metadata = ffmpeg.probe(file_path)["format"].get("tags", {})
        relative_path = os.path.relpath(file_path, "app").replace("\\", "/")
        return {
            "file_path": relative_path,
            "title": metadata.get("title") or "",
            "original_start_time": metadata.get("comment") or "",
            "username": metadata.get("artist") or "",
            "show": metadata.get("show") or "",
            "episode_number": metadata.get("episode_id") or "",
            "season_number": metadata.get("season_number") or "",
        }

    @staticmethod
    def get_videos_in_folder() -> list:
        folder = os.path.join(MEDIA_STATIC_PATH, "videos")
        return [
            Utils.get_video_in_folder(os.path.join(folder, file_name))
            for file_name in os.listdir(folder)
        ]

    def delete_file(self, file_path) -> bool:
        try:
            os.remove(f"./app/{file_path}")
            return True
        except OSError:
            return False
