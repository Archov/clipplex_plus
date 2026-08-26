from types import SimpleNamespace
import unittest
from unittest.mock import patch
import os

from app import app
from app.jobs import JobFailure, JobQueueFull
import clipplexAPI


class CreateVideoRouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.query = (
            "/create_video?username=alice&start_hour=00&start_minute=00&start_second=10"
            "&end_hour=00&end_minute=00&end_second=20"
        )

    @patch("app.routes.get_instant_video", return_value={"result": "success"})
    def test_success_response(self, create):
        response = self.client.post(
            self.query + "&audio_stream_id=audio-2&subtitle_stream_id=none"
            "&expected_media_identity=1&expected_session_id=session-1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"], "success")
        self.assertEqual(create.call_args.kwargs["audio_stream_id"], "audio-2")
        self.assertEqual(create.call_args.kwargs["subtitle_stream_id"], "none")
        self.assertEqual(create.call_args.kwargs["expected_media_identity"], "1")

    @patch("app.routes.clip_job_manager.enqueue", return_value="job-123")
    def test_json_request_uses_session_id_and_milliseconds(self, enqueue):
        response = self.client.post("/create_video", json={
            "session_id": "session-2",
            "media_identity": "22",
            "start_ms": 10123,
            "end_ms": 14567,
            "audio_stream_id": "audio-2",
            "subtitle_stream_id": "sub-3",
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job_id"], "job-123")
        queued = enqueue.call_args.args[0]
        self.assertEqual(queued["session_id"], "session-2")
        self.assertEqual(queued["start_ms"], 10123)
        self.assertEqual(queued["end_ms"], 14567)
        self.assertEqual(queued["media_identity"], "22")

    @patch("app.routes.clip_job_manager.enqueue", side_effect=JobQueueFull("Queue is full."))
    def test_json_request_returns_429_when_queue_is_full(self, enqueue):
        response = self.client.post("/create_video", json={
            "session_id": "session-2", "media_identity": "22",
            "start_ms": 1000, "end_ms": 2000,
        })

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["result"], "queue_full")

    @patch("app.routes.clip_job_manager.get")
    def test_job_status_endpoint_returns_progress_without_caching(self, get_job):
        get_job.return_value = {
            "job_id": "job-1", "status": "running", "stage": "rendering",
            "message": "Rendering.", "overall_progress": 75.5,
            "stage_progress": 64.2, "elapsed_ms": 1234,
            "queue_position": None, "result": None, "error": None,
        }

        response = self.client.get("/api/clip-jobs/job-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["stage"], "rendering")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch("app.routes.clip_job_manager.get", return_value=None)
    def test_unknown_or_expired_job_returns_404(self, get_job):
        response = self.client.get("/api/clip-jobs/missing")

        self.assertEqual(response.status_code, 404)
        self.assertIn("no longer available", response.get_json()["message"])

    @patch("app.routes.clipplexAPI.PlexSessions.list_video_sessions")
    def test_sessions_endpoint_does_not_expose_paths_or_tokens(self, sessions):
        sessions.return_value = [{
            "session_id": "one", "media_identity": "1", "username": "alice",
            "player_name": "TV", "player_product": "Plex", "state": "playing",
            "title": "Movie", "media_type": "movie", "view_offset_ms": 10123,
            "duration_ms": 60000,
        }]

        response = self.client.get("/api/sessions")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["sessions"][0]["view_offset_ms"], 10123)
        self.assertNotIn("media_path", response.get_data(as_text=True))
        self.assertNotIn("plex_token", response.get_data(as_text=True))

    @patch("app.routes.clipplexAPI.PlexInfo")
    def test_preview_uses_exact_session_identity_and_timestamp(self, plex_info):
        plex = SimpleNamespace(media_identity="1", preview_image=lambda at_ms: (b"jpeg", "image/jpeg"))
        plex_info.return_value = plex

        response = self.client.get(
            "/api/session-preview?session_id=session-1&media_identity=1&at_ms=12345"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"jpeg")
        plex_info.assert_called_once_with(session_id="session-1", inspect_media=False)

    @patch("app.routes.clipplexAPI.PlexInfo")
    def test_preview_rejects_stale_media_identity(self, plex_info):
        plex_info.return_value = SimpleNamespace(media_identity="current")

        response = self.client.get(
            "/api/session-preview?session_id=session-1&media_identity=old&at_ms=12345"
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("changed videos", response.get_json()["message"])

    @patch("app.routes.clip_job_manager.enqueue")
    def test_json_request_requires_media_identity(self, enqueue):
        response = self.client.post("/create_video", json={
            "session_id": "session-2", "start_ms": 1000, "end_ms": 2000,
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("media identity", response.get_json()["message"])
        enqueue.assert_not_called()

    def test_json_request_requires_an_object(self):
        response = self.client.post("/create_video", json=["not", "an", "object"])

        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.get_json()["message"])

    @patch("app.routes.clipplexAPI.Utils.get_videos_in_folder", return_value=[])
    def test_video_page_contains_session_picker_and_millisecond_fields(self, videos):
        response = self.client.get("/instant_video.html")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Active Plex Sessions", page)
        self.assertIn('id="start_time"', page)
        self.assertIn('id="capture_start"', page)
        self.assertIn('id="capture_end"', page)
        self.assertIn('id="refresh_position"', page)
        self.assertIn("HH:MM:SS.mmm", page)
        self.assertIn("/api/sessions", page)
        self.assertIn('role="radiogroup"', page)
        self.assertIn("moveSessionFocus", page)
        self.assertIn("'ArrowLeft'", page)
        self.assertIn("window.setInterval(renderLivePositionsWhenVisible, 100)", page)
        self.assertIn("document.hidden", page)
        self.assertNotIn("window.setInterval(renderLivePositions, 50)", page)
        self.assertIn('id="upload_modal"', page)
        self.assertIn("/api/uploaders/immich/options", page)
        self.assertIn("/api/uploads", page)
        self.assertIn("Export GIF", page)
        self.assertIn("/api/gif-exports", page)
        self.assertIn("/api/jobs/", page)
        self.assertIn("Make Clip", page)
        self.assertIn("Clip Library", page)
        self.assertIn('id="latest_delete_modal"', page)
        self.assertIn("slice(0, 1)", page)

    @patch("app.routes.clipplexAPI.Utils.get_videos_in_folder")
    def test_make_clip_page_renders_only_the_newest_clip(self, videos):
        videos.return_value = [
            {"file_path": "static/media/videos/new.mp4", "title": "Newest", "show": "", "original_start_time": "00:00:01.000", "username": "alice", "episode_number": "", "season_number": "", "display_heading": "Newest", "media_type": "movie"},
            {"file_path": "static/media/videos/old.mp4", "title": "Old", "show": "", "original_start_time": "00:00:01.000", "username": "alice", "episode_number": "", "season_number": "", "display_heading": "Old", "media_type": "movie"},
        ]

        page = self.client.get("/instant_video.html").get_data(as_text=True)

        self.assertIn("Newest", page)
        self.assertNotIn("static/media/videos/old.mp4", page)

    @patch("app.routes.clip_library.list_clips", return_value=[])
    def test_library_page_has_collapsible_filters_views_and_bottom_action(self, clips):
        page = self.client.get("/clip_library.html").get_data(as_text=True)

        self.assertIn('data-bs-target="#library_filters"', page)
        self.assertIn('id="grid_view"', page)
        self.assertIn('id="list_view"', page)
        self.assertIn('id="grid_size"', page)
        self.assertIn("/static/js/clip_library.js", page)
        self.assertIn('id="edit_custom_title"', page)
        self.assertLess(page.index('id="library_groups"'), page.index("Make a clip"))
        self.assertIn('aria-current="page"', page)

    @patch("app.routes.clip_library.save_clip_metadata")
    def test_clip_metadata_patch_returns_updated_clip(self, save):
        save.return_value = {"file_path": "static/media/videos/clip.mp4", "display_heading": "King Kong (1933)"}

        response = self.client.patch("/api/clips/metadata", json={
            "file_path": "static/media/videos/clip.mp4", "media_library": "Movies",
            "media_type": "movie", "title": "King Kong", "year": "1933",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["clip"]["display_heading"], "King Kong (1933)")

    @patch("app.routes.delete_generated_clip")
    def test_json_delete_endpoint_deletes_selected_clip(self, delete):
        response = self.client.delete("/api/clips", json={"file_path": "static/media/videos/clip.mp4"})

        self.assertEqual(response.status_code, 200)
        delete.assert_called_once_with("static/media/videos/clip.mp4")

    @patch("app.routes.clipplexAPI.Utils.get_videos_in_folder")
    def test_saved_clip_card_contains_gif_export_action(self, videos):
        videos.return_value = [{
            "file_path": "static/media/videos/clip.mp4", "title": "Clip", "show": "",
            "original_start_time": "00:00:01.000", "username": "alice",
            "episode_number": "", "season_number": "",
        }]

        page = self.client.get("/instant_video.html").get_data(as_text=True)

        self.assertIn('class="btn btn-outline-success gif-export"', page)
        self.assertIn('aria-label="Export GIF"', page)

    @patch("app.routes.get_instant_video")
    def test_track_failure_returns_retry_choices(self, create):
        plex = SimpleNamespace(
            media_identity="1",
            session_identifier="session-1",
            track_options=lambda: {
                "audio": [{"id": "audio-1", "available": True}],
                "subtitles": [{"id": "none", "available": True}],
            },
        )
        track = clipplexAPI.MediaTrack("sub-1", 3, "subtitle")
        create.side_effect = clipplexAPI.TrackSelectionError("Cannot burn selected subtitles.", plex, track)

        response = self.client.post(self.query)
        payload = response.get_json()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(payload["result"], "track_selection_required")
        self.assertEqual(payload["failed_track_id"], "sub-1")
        self.assertEqual(payload["media_identity"], "1")

    @patch("app.routes.get_instant_video", side_effect=clipplexAPI.StaleSessionError("Playback changed."))
    def test_stale_retry_is_rejected(self, create):
        response = self.client.post(self.query)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["result"], "stale_session")

    @patch(
        "app.routes.get_instant_video",
        side_effect=clipplexAPI.UnsupportedVideoError("Dolby Vision Profile 5 is unsupported."),
    )
    def test_legacy_request_returns_structured_unsupported_video_error(self, create):
        response = self.client.post(self.query)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["result"], "unsupported_video")

    @patch("app.routes.clip_library.allocate_clip_title", return_value={
        "source_key": "movie", "clip_number": 1,
        "clip_title": "Movie", "clip_title_custom": False,
    })
    @patch("app.routes.clip_library.ensure_thumbnail")
    @patch("app.routes.clip_library.save_clip_metadata", return_value={"file_path": "static/media/videos/clip.mp4"})
    @patch("app.routes.clipplexAPI.Video")
    @patch("app.routes.clipplexAPI.PlexInfo")
    def test_clip_creation_passes_fractional_range_to_video(self, plex_info, video, save_metadata, thumbnail, allocate_title):
        plex_info.return_value = SimpleNamespace(
            media_identity="1", session_identifier="session-1", duration_ms=60000,
            username="alice", media_title="Movie", media_path="/media/movie.mkv",
            resolve_tracks=lambda audio, subtitle: (
                clipplexAPI.MediaTrack("audio", 1, "audio"), None
            ),
        )

        result = __import__("app.routes", fromlist=["get_instant_video"]).get_instant_video(
            session_id="session-1", start_ms=10123, end_ms=14567,
            expected_media_identity="1", expected_session_id="session-1",
        )

        self.assertEqual(result["result"], "success")
        self.assertEqual(video.call_args.args[1], 10123)
        self.assertAlmostEqual(video.call_args.args[2], 4.444)
        progress_callback = video.return_value.extract_video.call_args.args[0]
        self.assertTrue(callable(progress_callback))
        self.assertEqual(video.return_value.metadata_clip_title, "Movie")
        allocate_title.assert_called_once()
        save_metadata.assert_called_once()
        saved_fields = save_metadata.call_args.args[1]
        self.assertEqual(saved_fields["clip_title"], "Movie")
        self.assertIs(saved_fields["original_start_time"], video.return_value.metadata_current_media_time)
        self.assertIs(saved_fields["original_end_time"], video.return_value.metadata_end_time)
        thumbnail.assert_called_once_with("static/media/videos/clip.mp4")

    @patch("app.routes.get_instant_video")
    def test_queued_track_recovery_preserves_original_payload(self, create):
        import app.routes as routes
        payload = {
            "session_id": "session-1", "media_identity": "media-1",
            "start_ms": 10123, "end_ms": 14567,
            "audio_stream_id": None, "subtitle_stream_id": None,
        }
        plex = SimpleNamespace(
            media_identity="media-1", session_identifier="session-1",
            track_options=lambda: {"audio": [], "subtitles": []},
        )
        track = clipplexAPI.MediaTrack("sub-1", 3, "subtitle")
        create.side_effect = clipplexAPI.TrackSelectionError("Cannot burn subtitles.", plex, track)

        with self.assertRaises(JobFailure) as raised:
            routes.run_clip_job(payload, lambda *args: None)

        self.assertEqual(raised.exception.status, "recovery_required")
        retry = raised.exception.error["retry_payload"]
        self.assertEqual(retry["start_ms"], 10123)
        self.assertEqual(retry["end_ms"], 14567)
        self.assertEqual(retry["media_identity"], "media-1")

    @patch("app.routes.get_instant_video", side_effect=clipplexAPI.UnsupportedVideoError("Unsupported Dolby Vision."))
    def test_queued_unsupported_video_is_a_failed_job_not_track_recovery(self, create):
        import app.routes as routes
        payload = {
            "session_id": "session-1", "media_identity": "media-1",
            "start_ms": 1000, "end_ms": 2000,
            "audio_stream_id": None, "subtitle_stream_id": None,
        }

        with self.assertRaises(JobFailure) as raised:
            routes.run_clip_job(payload, lambda *args: None)

        self.assertEqual(raised.exception.status, "failed")
        self.assertEqual(raised.exception.error["result"], "unsupported_video")

    def test_uploader_endpoint_exposes_only_configured_service_metadata(self):
        with patch.dict(os.environ, {
            "STREAMABLE_LOGIN": "user", "STREAMABLE_PASSWORD": "password",
            "IMMICH_URL": "http://immich", "IMMICH_API_KEY": "secret",
        }, clear=True):
            response = self.client.get("/api/uploaders")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.get_json()["uploaders"]], ["streamable", "immich"])
        self.assertNotIn("password", response.get_data(as_text=True))
        self.assertNotIn("secret", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch("app.routes.uploaders.ImmichUploader")
    @patch("app.routes.uploaders.configured_uploader_ids", return_value={"immich"})
    def test_immich_options_endpoint(self, configured, immich):
        immich.return_value.options.return_value = {
            "tags": [{"id": "tag-1", "name": "Clip"}],
            "albums": [{"id": "album-1", "name": "Shared"}],
            "default_tag": "#plex-clip",
        }

        response = self.client.get("/api/uploaders/immich/options")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["default_tag"], "#plex-clip")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch("app.routes.uploaders.upload_clip")
    def test_unified_upload_route_forwards_immich_metadata(self, upload):
        upload.return_value = ({
            "result": "success", "uploader": "immich", "asset_id": "asset-1"
        }, 200)

        response = self.client.post("/api/uploads", json={
            "file_path": "static/media/videos/clip.mp4",
            "uploader": "immich",
            "tag_ids": ["tag-1", "tag-1"],
            "tag_names": ["new-tag"],
            "album_ids": ["album-1", "album-2"],
            "new_album_name": "New Clips",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upload.call_args.kwargs["tag_ids"], ["tag-1"])
        self.assertEqual(upload.call_args.kwargs["tag_names"], ["new-tag"])
        self.assertEqual(upload.call_args.kwargs["album_ids"], ["album-1", "album-2"])
        self.assertEqual(upload.call_args.kwargs["new_album_name"], "New Clips")

    @patch("app.routes.uploaders.upload_clip")
    def test_unified_upload_route_preserves_partial_success_status(self, upload):
        upload.return_value = ({
            "result": "partial_success", "uploader": "immich",
            "asset_id": "asset-1", "failures": [{"step": "tags", "message": "Denied"}],
        }, 207)

        response = self.client.post("/api/uploads", json={
            "file_path": "static/media/videos/clip.mp4", "uploader": "immich",
        })

        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.get_json()["result"], "partial_success")

    def test_unified_upload_route_validates_json_lists(self):
        response = self.client.post("/api/uploads", json={
            "file_path": "static/media/videos/clip.mp4",
            "uploader": "immich",
            "tag_ids": "not-a-list",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("tag_ids", response.get_json()["message"])

    @patch("app.routes.uploaders.upload_clip")
    def test_legacy_streamable_route_uses_shared_uploader(self, upload):
        upload.return_value = ({"result": "success", "shortcode": "abc"}, 200)

        response = self.client.post(
            "/streamable_upload?file_path=static/media/videos/clip.mp4"
        )

        self.assertEqual(response.status_code, 200)
        upload.assert_called_once_with(
            file_path="static/media/videos/clip.mp4", uploader="streamable"
        )

    @patch("app.routes.gif_exports.cached_export")
    def test_gif_export_returns_fresh_cached_file_without_queueing(self, cached):
        cached.return_value = {
            "download_url": "/static/media/gifs/clip.gif",
            "filename": "clip.gif", "size_bytes": 123, "cached": True,
        }

        response = self.client.post("/api/gif-exports", json={
            "file_path": "static/media/videos/clip.mp4",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["export"]["cached"])

    @patch("app.routes.clip_job_manager.enqueue", return_value="gif-job-1")
    @patch("app.routes.gif_exports.cached_export", return_value=None)
    def test_gif_export_queues_conversion_with_generic_status_url(self, cached, enqueue):
        response = self.client.post("/api/gif-exports", json={
            "file_path": "static/media/videos/clip.mp4",
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job_type"], "gif_export")
        self.assertEqual(response.get_json()["status_url"], "/api/jobs/gif-job-1")
        self.assertEqual(enqueue.call_args.args[0]["job_type"], "gif_export")

    @patch("app.routes.clip_job_manager.enqueue", side_effect=JobQueueFull("Queue is full."))
    @patch("app.routes.gif_exports.cached_export", return_value=None)
    def test_gif_export_returns_429_when_shared_queue_is_full(self, cached, enqueue):
        response = self.client.post("/api/gif-exports", json={
            "file_path": "static/media/videos/clip.mp4",
        })

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["result"], "queue_full")

    @patch("app.routes.gif_exports.cached_export")
    def test_gif_export_preserves_validation_status(self, cached):
        cached.side_effect = __import__(
            "app.gif_exports", fromlist=["GifExportError"]
        ).GifExportError("Only saved clips may be exported.", 400)

        response = self.client.post("/api/gif-exports", json={"file_path": "../secret"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["result"], "error")

    @patch("app.routes.gif_exports.export_gif")
    def test_media_worker_dispatches_gif_jobs(self, export):
        export.return_value = {"result": "success", "export": {"filename": "clip.gif"}}

        import app.routes as routes
        result = routes.run_media_job(
            {"job_type": "gif_export", "file_path": "static/media/videos/clip.mp4"},
            lambda *args: None,
        )

        self.assertEqual(result["export"]["filename"], "clip.gif")


if __name__ == "__main__":
    unittest.main()
