from fractions import Fraction
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import ffmpeg
import requests

import clipplexAPI


SESSION_XML = b"""
<MediaContainer size="1">
  <Video key="/library/metadata/1" ratingKey="1" sessionKey="9" viewOffset="65000" type="movie">
    <User title="alice" />
    <Session id="session-1" />
    <Media id="old-media"><Part id="11" /></Media>
    <Media id="active-media" selected="1">
      <Part id="22" selected="1">
        <Stream id="video" index="0" streamType="1" frameRate="24" />
        <Stream id="audio-1" index="1" streamType="2" />
        <Stream id="audio-2" index="2" streamType="2" selected="1" />
        <Stream id="sub-text" index="3" streamType="3" />
        <Stream id="sub-pgs" index="4" streamType="3" selected="1" />
      </Part>
    </Media>
  </Video>
</MediaContainer>
"""

METADATA_XML = b"""
<MediaContainer size="1">
  <Video ratingKey="1" type="movie" title="Test Movie">
    <Media id="old-media"><Part id="11" file="/media/old.mkv" /></Media>
    <Media id="active-media">
      <Part id="22" file="/media/active.mkv">
        <Stream id="video" index="0" streamType="1" codec="h264" frameRate="24" />
        <Stream id="audio-1" index="1" streamType="2" codec="aac" language="English" title="Stereo" />
        <Stream id="audio-2" index="2" streamType="2" codec="ac3" language="Japanese" title="5.1" />
        <Stream id="sub-text" index="3" streamType="3" codec="srt" language="English" />
        <Stream id="sub-pgs" index="4" streamType="3" codec="hdmv_pgs_subtitle" language="Japanese" />
      </Part>
    </Media>
  </Video>
</MediaContainer>
"""

PROBE = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3"},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip"},
        {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
    ]
}


class FakeResponse:
    def __init__(self, content, headers=None):
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        return None


class PlexInfoTests(unittest.TestCase):
    def build_plex(self, session_xml=SESSION_XML, metadata_xml=METADATA_XML):
        responses = [FakeResponse(session_xml), FakeResponse(metadata_xml)]
        with patch.dict(os.environ, {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "secret"}), \
             patch("clipplexAPI.requests.get", side_effect=responses) as get, \
             patch("clipplexAPI.ffmpeg.probe", return_value=PROBE):
            plex = clipplexAPI.PlexInfo("alice")
        self.assertEqual(get.call_args_list[0].args[0], "http://plex:32400/status/sessions")
        return plex

    def test_uses_active_media_part_and_playing_track_selection(self):
        plex = self.build_plex()

        self.assertEqual(plex.media_path, "/media/active.mkv")
        self.assertEqual(plex.session_identifier, "session-1")
        audio, subtitle = plex.resolve_tracks()
        self.assertEqual(audio.id, "audio-2")
        self.assertEqual(audio.index, 2)
        self.assertEqual(subtitle.id, "sub-pgs")
        self.assertEqual(subtitle.subtitle_index, 1)
        self.assertEqual(subtitle.probe_codec, "hdmv_pgs_subtitle")
        self.assertTrue(subtitle.graphical)

    def test_resolves_exact_session_id_instead_of_first_matching_user(self):
        second = SESSION_XML.replace(b'session-1', b'session-2').replace(b'viewOffset="65000"', b'viewOffset="70123"')
        combined = SESSION_XML.replace(b'size="1"', b'size="2"').replace(
            b'</MediaContainer>', second.split(b'\n', 2)[2].replace(b'</MediaContainer>', b'') + b'</MediaContainer>'
        )
        responses = [FakeResponse(combined), FakeResponse(METADATA_XML)]
        with patch.dict(os.environ, {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "secret"}), \
             patch("clipplexAPI.requests.get", side_effect=responses), \
             patch("clipplexAPI.ffmpeg.probe", return_value=PROBE):
            plex = clipplexAPI.PlexInfo(session_id="session-2")

        self.assertEqual(plex.session_identifier, "session-2")
        self.assertEqual(plex.current_media_time_int, 70123)

    def test_lightweight_session_list_includes_all_videos_and_milliseconds(self):
        sessions_xml = ET.fromstring(b"""
        <MediaContainer>
          <Video ratingKey="1" viewOffset="10123" duration="90000" type="movie" title="Movie A">
            <User title="alice"/><Player title="Living Room" product="Plex for TV" state="playing"/>
            <Session id="one"/><Media><Part id="1"/></Media>
          </Video>
          <Video ratingKey="2" sessionKey="fallback-two" viewOffset="20456" duration="120000" type="episode" title="Episode">
            <User title="alice"/><Player title="Browser" state="paused"/>
            <Media><Part id="2"/></Media>
          </Video>
          <Track ratingKey="3" viewOffset="1"><User title="bob"/></Track>
        </MediaContainer>
        """)

        sessions = clipplexAPI.PlexSessions().list_video_sessions(sessions_xml)

        self.assertEqual([session["session_id"] for session in sessions], ["one", "fallback-two"])
        self.assertEqual(sessions[0]["view_offset_ms"], 10123)
        self.assertEqual(sessions[0]["media_identity"], "1:1")
        self.assertEqual(sessions[1]["state"], "paused")
        self.assertNotIn("media_path", sessions[0])
        self.assertNotIn("plex_token", sessions[0])

    def test_fallback_session_id_stays_stable_when_media_changes(self):
        first = ET.fromstring(
            '<Video ratingKey="1"><User title="alice"/><Player title="TV" machineIdentifier="player-1"/>'
            '<Media id="media-1"><Part id="part-1"/></Media></Video>'
        )
        second = ET.fromstring(
            '<Video ratingKey="2"><User title="alice"/><Player title="TV" machineIdentifier="player-1"/>'
            '<Media id="media-2"><Part id="part-2"/></Media></Video>'
        )

        self.assertEqual(
            clipplexAPI.PlexSessions.session_identifier(first),
            clipplexAPI.PlexSessions.session_identifier(second),
        )
        self.assertNotEqual(
            clipplexAPI.PlexSessions.media_identity(first),
            clipplexAPI.PlexSessions.media_identity(second),
        )

    def test_ffprobe_codec_classifies_plex_pgs_alias_as_graphical(self):
        metadata_xml = METADATA_XML.replace(b'codec="hdmv_pgs_subtitle"', b'codec="pgs"')
        plex = self.build_plex(metadata_xml=metadata_xml)

        _, subtitle = plex.resolve_tracks()
        self.assertEqual(subtitle.codec, "pgs")
        self.assertEqual(subtitle.probe_codec, "hdmv_pgs_subtitle")
        self.assertTrue(subtitle.graphical)

    def test_clip_only_overrides_are_validated_against_active_part(self):
        plex = self.build_plex()

        audio, subtitle = plex.resolve_tracks("audio-1", "sub-text")
        self.assertEqual((audio.id, subtitle.id), ("audio-1", "sub-text"))
        self.assertEqual(subtitle.subtitle_index, 0)
        _, subtitle = plex.resolve_tracks("audio-1", "none")
        self.assertIsNone(subtitle)
        with self.assertRaises(clipplexAPI.TrackSelectionError):
            plex.resolve_tracks("missing", "none")

    def test_no_selected_subtitle_means_off(self):
        plex = self.build_plex(SESSION_XML.replace(b' selected="1" />\n      </Part>', b' />\n      </Part>'))

        _, subtitle = plex.resolve_tracks()
        self.assertIsNone(subtitle)
        self.assertTrue(plex.track_options()["subtitles"][0]["selected"])

    def test_missing_selected_stream_returns_recovery_options(self):
        probe = {"streams": [stream for stream in PROBE["streams"] if stream["index"] != 4]}
        responses = [FakeResponse(SESSION_XML), FakeResponse(METADATA_XML)]
        with patch.dict(os.environ, {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "secret"}), \
             patch("clipplexAPI.requests.get", side_effect=responses), \
             patch("clipplexAPI.ffmpeg.probe", return_value=probe):
            plex = clipplexAPI.PlexInfo("alice")

        with self.assertRaises(clipplexAPI.TrackSelectionError) as raised:
            plex.resolve_tracks()
        self.assertEqual(raised.exception.failed_track.id, "sub-pgs")
        pgs_option = next(option for option in plex.track_options()["subtitles"] if option["id"] == "sub-pgs")
        self.assertFalse(pgs_option["available"])


class VideoCommandTests(unittest.TestCase):
    def make_plex(self, video_stream=None):
        stream = {"index": 0, "codec_type": "video"}
        if video_stream:
            stream.update(video_stream)
        return SimpleNamespace(
            media_path="input.mkv",
            metadata_element=ET.fromstring('<Video type="movie" title="Movie" />'),
            media_type="movie",
            current_media_time_str="00:01:00",
            username="alice",
            video_index=0,
            plex_url="http://plex:32400",
            plex_token="secret",
            headers={"X-Plex-Token": "secret"},
            probe={"streams": [stream]},
        )

    def compiled(self, subtitle_track=None, subtitle_path=None, subtitle_fonts_dir=None):
        audio = clipplexAPI.MediaTrack("audio-2", 2, "audio", codec="ac3")
        video = clipplexAPI.Video(self.make_plex(), "00:01:00", 10, "clip", audio, subtitle_track)
        if subtitle_path is None and subtitle_track and subtitle_track.external:
            subtitle_path = "subtitle.srt"
        return " ".join(ffmpeg.compile(video.build_ffmpeg(subtitle_path, subtitle_fonts_dir)))

    def test_maps_exact_audio_and_burns_extracted_text_subtitle(self):
        subtitle = clipplexAPI.MediaTrack("sub", 3, "subtitle", codec="srt", subtitle_index=0)
        command = self.compiled(subtitle, "selected.ass", "fonts")

        self.assertIn("[0:2]asetpts=PTS-STARTPTS", command)
        self.assertIn("subtitles=selected.ass:fontsdir=fonts", command)
        self.assertIn("setpts=PTS+30.0/TB", command)
        self.assertIn("-acodec aac", command)
        self.assertIn("-ac 2", command)
        self.assertIn("-ar 48000", command)
        self.assertIn("-profile:a aac_low", command)
        self.assertNotIn("-c:s", command)

    def test_embedded_text_extract_maps_exact_stream_and_clip_interval(self):
        subtitle = clipplexAPI.MediaTrack("sub", 3, "subtitle", codec="ass", subtitle_index=0)
        video = clipplexAPI.Video(
            self.make_plex(), "00:01:00", 10, "clip",
            clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle,
        )

        command = " ".join(ffmpeg.compile(video.build_text_subtitle_extract_ffmpeg("selected.ass")))

        self.assertIn("-ss 30.0", command)
        self.assertIn("-t 40.0", command)
        self.assertIn("-map 0:3", command)
        self.assertIn("-scodec ass", command)

    def test_text_subtitle_preroll_is_clamped_at_media_start(self):
        subtitle = clipplexAPI.MediaTrack("sub", 3, "subtitle", codec="ass", subtitle_index=0)
        video = clipplexAPI.Video(
            self.make_plex(), 5123, 2.345, "clip",
            clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle,
        )

        extract_command = " ".join(ffmpeg.compile(video.build_text_subtitle_extract_ffmpeg("selected.ass")))
        burn_command = " ".join(ffmpeg.compile(video.build_ffmpeg("selected.ass")))

        self.assertIn("-ss 0.0", extract_command)
        self.assertIn("-t 7.468", extract_command)
        self.assertIn("setpts=PTS+5.123/TB", burn_command)

    def test_external_text_subtitle_uses_downloaded_file(self):
        subtitle = clipplexAPI.MediaTrack("external", None, "subtitle", codec="srt", key="/library/streams/3")
        command = self.compiled(subtitle)
        self.assertIn("subtitles=subtitle.srt", command)

    @unittest.skipUnless(os.name == "nt", "Windows libass path handling")
    def test_windows_subtitle_filter_uses_a_relative_temp_path(self):
        subtitle = clipplexAPI.MediaTrack("sub", 3, "subtitle", codec="ass", subtitle_index=0)
        command = self.compiled(subtitle, str(Path.cwd() / "selected.ass"), str(Path.cwd() / "fonts"))

        self.assertIn("subtitles=selected.ass:fontsdir=fonts", command)
        self.assertNotIn(str(Path.cwd()).replace("\\", "/"), command)

    def test_graphical_subtitle_uses_exact_stream_overlay(self):
        subtitle = clipplexAPI.MediaTrack(
            "pgs", 4, "subtitle", codec="pgs", subtitle_index=1, probe_codec="hdmv_pgs_subtitle"
        )
        with patch.object(clipplexAPI.Video, "_graphical_subtitle_seek_start", return_value=58.5):
            command = self.compiled(subtitle)
        self.assertIn("[0:0][0:4]overlay=eof_action=pass:repeatlast=0", command)
        self.assertIn("trim=duration=10.0:start=1.5", command)
        self.assertIn("atrim=duration=10.0:start=1.5", command)
        self.assertIn("-ss 58.5", command)
        self.assertIn("-preset veryfast", command)

    def test_graphical_preroll_starts_before_latest_packet_cluster(self):
        subtitle = clipplexAPI.MediaTrack(
            "pgs", 4, "subtitle", codec="pgs", probe_codec="hdmv_pgs_subtitle"
        )
        video = clipplexAPI.Video(
            self.make_plex(), "00:05:00", 10, "clip",
            clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle,
        )
        packets = {"packets": [
            {"pts_time": "285.0"},
            {"pts_time": "298.0"},
            {"pts_time": "298.2"},
            {"pts_time": "301.0"},
        ]}

        with patch("clipplexAPI.ffmpeg.probe", return_value=packets) as probe:
            seek_start = video._graphical_subtitle_seek_start()

        self.assertEqual(seek_start, 297.5)
        self.assertEqual(probe.call_args.kwargs["select_streams"], "4")
        self.assertEqual(probe.call_args.kwargs["read_intervals"], "180.0%+121.0")

    def test_fractional_start_and_duration_are_preserved(self):
        video = clipplexAPI.Video(
            self.make_plex(), 60123, 2.345, "clip",
            clipplexAPI.MediaTrack("audio", 1, "audio"),
        )

        command = " ".join(ffmpeg.compile(video.build_ffmpeg()))

        self.assertIn("-ss 60.123", command)
        self.assertIn("-t 2.345", command)
        self.assertIn("comment=00:01:00.123", command)

    def test_hdr10_is_tone_mapped_scaled_and_tagged_as_bt709(self):
        plex = self.make_plex({
            "color_transfer": "smpte2084", "color_primaries": "bt2020",
            "color_space": "bt2020nc", "color_range": "tv",
        })
        video = clipplexAPI.Video(
            plex, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
        )

        command = " ".join(ffmpeg.compile(video.build_ffmpeg()))

        self.assertTrue(video.color_info.is_hdr)
        self.assertIn("tin=smpte2084", command)
        self.assertIn("tonemap=desat=2:param=0.3:tonemap=mobius", command)
        self.assertIn("d=error_diffusion:m=bt709:p=bt709:r=tv:t=bt709", command)
        self.assertIn("scale=min(1920\\,iw):min(1080\\,ih)", command)
        self.assertIn("-color_primaries bt709", command)
        self.assertIn("-color_trc bt709", command)
        self.assertIn("-colorspace bt709", command)
        self.assertIn("-color_range tv", command)
        self.assertIn("-x264-params colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited", command)
        self.assertIn("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv", command)

    def test_hlg_is_detected_and_incomplete_hdr_uses_safe_defaults(self):
        plex = self.make_plex({"color_transfer": "arib-std-b67"})
        video = clipplexAPI.Video(
            plex, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
        )

        command = " ".join(ffmpeg.compile(video.build_ffmpeg()))

        self.assertTrue(video.color_info.is_hdr)
        self.assertIn("tin=arib-std-b67", command)
        self.assertIn("pin=bt2020", command)
        self.assertIn("min=bt2020nc", command)
        self.assertIn("rin=tv", command)

    def test_sdr_is_scaled_without_tone_mapping_or_hdr_tag_override(self):
        plex = self.make_plex({
            "color_transfer": "bt709", "color_primaries": "bt709",
            "color_space": "bt709", "color_range": "tv",
        })
        video = clipplexAPI.Video(
            plex, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
        )

        command = " ".join(ffmpeg.compile(video.build_ffmpeg()))

        self.assertFalse(video.color_info.is_hdr)
        self.assertNotIn("tonemap=", command)
        self.assertIn("scale=min(1920\\,iw):min(1080\\,ih)", command)
        self.assertNotIn("-color_primaries", command)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and FFprobe are required for scaler integration coverage",
    )
    def test_output_is_bounded_without_upscaling_letterboxing_or_distortion(self):
        fixtures = (
            ("smaller", 640, 360, "1/1", (640, 360), Fraction(16, 9)),
            ("larger", 2560, 1440, "1/1", (1920, 1080), Fraction(16, 9)),
            ("non_16_9", 2048, 1536, "1/1", (1440, 1080), Fraction(4, 3)),
            ("odd_dimensions", 641, 359, "1/1", (640, 358), Fraction(641, 359)),
            ("non_square_sar", 720, 480, "8/9", (720, 480), Fraction(4, 3)),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            for name, width, height, sar, expected_size, expected_dar in fixtures:
                with self.subTest(name=name):
                    source_path = Path(temp_dir) / f"{name}-source.mkv"
                    output_path = Path(temp_dir) / f"{name}-output.mkv"
                    source = ffmpeg.input(
                        f"testsrc=size={width}x{height}:rate=1:duration=0.1", f="lavfi"
                    ).filter("setsar", sar)
                    (
                        ffmpeg.output(
                            source,
                            str(source_path),
                            vcodec="ffv1",
                            pix_fmt="yuv444p",
                            vframes=1,
                        )
                        .overwrite_output()
                        .run(quiet=True)
                    )

                    media = ffmpeg.input(str(source_path))
                    scaled = clipplexAPI.Video._scale_for_compatibility(media.video)
                    (
                        ffmpeg.output(
                            scaled,
                            str(output_path),
                            vcodec="ffv1",
                            pix_fmt="yuv444p",
                            vframes=1,
                        )
                        .overwrite_output()
                        .run(quiet=True)
                    )

                    stream = ffmpeg.probe(str(output_path), select_streams="v:0")["streams"][0]
                    actual_size = (stream["width"], stream["height"])
                    actual_dar = Fraction(stream["display_aspect_ratio"].replace(":", "/"))

                    self.assertEqual(actual_size, expected_size)
                    self.assertEqual(actual_dar, expected_dar)
                    self.assertLessEqual(actual_size[0], width)
                    self.assertLessEqual(actual_size[1], height)
                    self.assertEqual(actual_size[0] % 2, 0)
                    self.assertEqual(actual_size[1] % 2, 0)

    def test_text_subtitles_are_burned_after_tone_mapping_and_scaling(self):
        plex = self.make_plex({"color_transfer": "smpte2084"})
        subtitle = clipplexAPI.MediaTrack("sub", 3, "subtitle", codec="ass", subtitle_index=0)
        video = clipplexAPI.Video(
            plex, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle
        )

        command = " ".join(ffmpeg.compile(video.build_ffmpeg("selected.ass")))

        self.assertLess(command.index("tonemap="), command.index("scale=min(1920"))
        self.assertLess(command.index("scale=min(1920"), command.index("subtitles="))

    def test_graphical_subtitles_overlay_after_tone_mapping_and_scale_with_composition(self):
        plex = self.make_plex({"color_transfer": "smpte2084"})
        subtitle = clipplexAPI.MediaTrack(
            "pgs", 4, "subtitle", codec="pgs", probe_codec="hdmv_pgs_subtitle"
        )
        video = clipplexAPI.Video(
            plex, 60_000, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle
        )

        with patch.object(video, "_graphical_subtitle_seek_start", return_value=58.5):
            command = " ".join(ffmpeg.compile(video.build_ffmpeg()))

        self.assertLess(command.index("tonemap="), command.index("overlay="))
        self.assertLess(command.index("overlay="), command.index("scale=min(1920"))

    def test_compatible_dolby_vision_base_layer_uses_hdr_pipeline(self):
        plex = self.make_plex({
            "color_transfer": "smpte2084",
            "side_data_list": [{
                "side_data_type": "DOVI configuration record", "dv_profile": 8,
                "dv_bl_signal_compatibility_id": 1,
            }],
        })

        video = clipplexAPI.Video(
            plex, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
        )

        self.assertTrue(video.color_info.dolby_vision)
        self.assertTrue(video.color_info.is_hdr)

    def test_profile_5_and_ambiguous_dolby_vision_are_rejected(self):
        profile_five = self.make_plex({
            "color_transfer": "smpte2084",
            "side_data_list": [{
                "side_data_type": "DOVI configuration record", "dv_profile": 5,
                "dv_bl_signal_compatibility_id": 0,
            }],
        })
        ambiguous = self.make_plex({
            "codec_tag_string": "dvhe", "color_transfer": "smpte2084",
        })

        with self.assertRaises(clipplexAPI.UnsupportedVideoError):
            clipplexAPI.Video(
                profile_five, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
            )
        with self.assertRaises(clipplexAPI.UnsupportedVideoError):
            clipplexAPI.Video(
                ambiguous, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
            )

    def test_hdr_filter_failure_is_not_reported_as_an_audio_track_failure(self):
        plex = self.make_plex({"color_transfer": "smpte2084"})
        audio = clipplexAPI.MediaTrack("audio", 1, "audio")
        video = clipplexAPI.Video(plex, 0, 10, "clip", audio)
        error = ffmpeg.Error("ffmpeg", b"", b"zscale: no path between colorspaces")

        with patch("clipplexAPI.run_ffmpeg_with_progress", side_effect=error):
            with self.assertRaises(clipplexAPI.VideoConversionError):
                video.extract_video()

        self.assertTrue(audio.available)

    def test_hdr_render_progress_names_tone_mapping(self):
        plex = self.make_plex({"color_transfer": "smpte2084"})
        video = clipplexAPI.Video(
            plex, 0, 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio")
        )
        updates = []

        with patch("clipplexAPI.run_ffmpeg_with_progress", return_value=None):
            video.extract_video(lambda *update: updates.append(update))

        rendering = [update for update in updates if update[0] == "rendering"]
        self.assertTrue(rendering)
        self.assertIn("Tone-mapping HDR", rendering[0][3])

    def test_external_subtitle_is_removed_after_encoding_failure(self):
        subtitle = clipplexAPI.MediaTrack("external", None, "subtitle", codec="srt", key="/library/streams/3")
        video = clipplexAPI.Video(
            self.make_plex(), "00:01:00", 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle
        )
        temporary_path = Path("external-test.srt").resolve()
        temporary_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
        try:
            with patch.object(video, "_download_external_subtitle", return_value=str(temporary_path)), \
                 patch("clipplexAPI.run_ffmpeg_with_progress", side_effect=ffmpeg.Error("ffmpeg", b"", b"failed")):
                with self.assertRaises(clipplexAPI.TrackSelectionError):
                    video.extract_video()
            self.assertFalse(temporary_path.exists())
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def test_external_subtitle_download_uses_token_header(self):
        subtitle = clipplexAPI.MediaTrack("external", None, "subtitle", codec="srt", key="/library/streams/3")
        video = clipplexAPI.Video(
            self.make_plex(), "00:01:00", 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle
        )
        response = FakeResponse(b"subtitle data")
        with patch("clipplexAPI.requests.get", return_value=response) as get:
            subtitle_path = Path(video._download_external_subtitle())
        try:
            self.assertEqual(subtitle_path.read_bytes(), b"subtitle data")
            self.assertEqual(get.call_args.kwargs["headers"], {"X-Plex-Token": "secret"})
            self.assertTrue(get.call_args.kwargs["stream"])
            self.assertNotIn("secret", get.call_args.args[0])
        finally:
            subtitle_path.unlink(missing_ok=True)

    def test_partial_external_subtitle_download_is_removed(self):
        subtitle = clipplexAPI.MediaTrack("external", None, "subtitle", codec="srt", key="/library/streams/3")
        video = clipplexAPI.Video(
            self.make_plex(), "00:01:00", 10, "clip", clipplexAPI.MediaTrack("audio", 1, "audio"), subtitle
        )
        response = FakeResponse(b"", headers={"Content-Length": "100"})

        def failing_chunks(chunk_size):
            yield b"partial"
            raise requests.ConnectionError("download stopped")

        response.iter_content = failing_chunks
        with tempfile.TemporaryDirectory() as temporary_directory:
            created_path = None
            real_named_temporary_file = tempfile.NamedTemporaryFile

            def capture_temporary_file(*args, **kwargs):
                nonlocal created_path
                kwargs["dir"] = temporary_directory
                temporary_file = real_named_temporary_file(*args, **kwargs)
                created_path = Path(temporary_file.name)
                return temporary_file

            with patch("clipplexAPI.requests.get", return_value=response), \
                    patch("clipplexAPI.tempfile.NamedTemporaryFile", side_effect=capture_temporary_file):
                with self.assertRaises(requests.ConnectionError):
                    video._download_external_subtitle()

            self.assertIsNotNone(created_path)
            self.assertFalse(created_path.exists())

    def test_snapshot_ffmpeg_treats_media_path_as_one_argument(self):
        media_path = "/data/media/movie; touch should-not-run.mkv"

        with patch("clipplexAPI.subprocess.call") as call:
            clipplexAPI.Snapshot(media_path, "00:01:02.345", 24)._download_frames()

        command = call.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[command.index("-i") + 1], media_path)
        self.assertNotIn("shell", call.call_args.kwargs)
        self.assertEqual(command[command.index("-vframes") + 1], "24")

    def test_audio_encoding_failure_returns_audio_recovery(self):
        audio = clipplexAPI.MediaTrack("audio", 1, "audio")
        video = clipplexAPI.Video(self.make_plex(), "00:01:00", 10, "clip", audio)
        with patch("clipplexAPI.run_ffmpeg_with_progress", side_effect=ffmpeg.Error("ffmpeg", b"", b"failed")):
            with self.assertRaises(clipplexAPI.TrackSelectionError) as raised:
                video.extract_video()
        self.assertIs(raised.exception.failed_track, audio)
        self.assertFalse(audio.available)


class FFmpegProgressTests(unittest.TestCase):
    class Process:
        def __init__(self, lines, return_code=0):
            self.stdout = iter(lines)
            self.return_code = return_code

        def wait(self):
            return self.return_code

    @patch("clipplexAPI.subprocess.Popen")
    @patch("clipplexAPI.ffmpeg.compile", return_value=["ffmpeg", "-i", "input.mkv", "output.mp4"])
    def test_media_time_progress_is_monotonic_and_clamped(self, compile_graph, popen):
        popen.return_value = self.Process([
            "out_time_us=5000000\n",
            "out_time_ms=2000000\n",
            "out_time_us=12000000\n",
            "progress=end\n",
        ])
        updates = []

        clipplexAPI.run_ffmpeg_with_progress(object(), 10, updates.append)

        self.assertTrue(all(left <= right for left, right in zip(updates, updates[1:])))
        self.assertEqual(updates[0], 50.0)
        self.assertEqual(updates[-1], 100.0)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:6], ["-progress", "pipe:1", "-nostats", "-loglevel", "error"])

    @patch("clipplexAPI.subprocess.Popen")
    @patch("clipplexAPI.ffmpeg.compile", return_value=["ffmpeg", "-i", "missing.mkv", "output.mp4"])
    def test_ffmpeg_failure_preserves_error_output(self, compile_graph, popen):
        popen.return_value = self.Process([
            "out_time_us=1000000\n",
            "input.mkv: Invalid data found when processing input\n",
        ], return_code=1)

        with self.assertRaises(ffmpeg.Error) as raised:
            clipplexAPI.run_ffmpeg_with_progress(object(), 10)

        self.assertIn(b"Invalid data", raised.exception.stderr)


class UtilsTests(unittest.TestCase):
    def test_formats_and_parses_millisecond_timestamps(self):
        self.assertEqual(clipplexAPI.Utils.milliseconds_to_string(3_723_004), "01:02:03.004")
        self.assertEqual(clipplexAPI.Utils.time_to_milliseconds("01:02:03.004"), 3_723_004)
        self.assertEqual(clipplexAPI.Utils.time_to_milliseconds("01:02:03.4"), 3_723_400)
        self.assertEqual(clipplexAPI.Utils().add_time("00:00:01.250", 15), "00:00:16.250")

    def test_subtitle_temp_directory_is_accessible_and_removable(self):
        directory = clipplexAPI.make_temporary_directory("clipplex-test-subtitle-")
        marker = Path(directory, "marker.txt")
        try:
            marker.write_text("ok", encoding="utf-8")
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")
        finally:
            import shutil
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
