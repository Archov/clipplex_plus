import sqlite3
import time
import ffmpeg
from flask import render_template, redirect, request, jsonify, Response, send_file
from app import app
from app.forms import video as formVideo
from app.jobs import ClipJobManager, JobFailure, JobQueueFull
from app import clip_library, clip_trims, gif_exports, settings, uploaders
from app.media_files import MediaFileError, delete_generated_clip
import clipplexAPI

@app.route("/")
def home():
    return redirect("/instant_video.html")

@app.route("/instant_snapshot.html", methods=["GET"])
def instant_snapshot():
    return render_template("instant_snapshot.html", title="Instant Snapshot", images=clipplexAPI.Utils.get_images_in_folder())

@app.route("/get_instant_snapshot", methods=["GET"])
def get_instant_snapshot():
    plex_data = clipplexAPI.PlexInfo("jonike") #DEBUG
    snapshot = clipplexAPI.Snapshot(plex_data.media_path, plex_data.current_media_time_str, plex_data.media_fps)
    snapshot._download_frames()
    return "Files downloaded"

@app.route("/get_current_stream", methods=["GET", "POST"])
def get_current_stream():
    username = request.args.get("username")
    try:
        plex = clipplexAPI.PlexInfo(username)
    except:
        return {"message": f"No session running for user {username}"}
    return {"file_path": str(plex.media_path), "username": username, "current_time": plex.current_media_time_str, "media_title": plex.media_title}


@app.route("/api/sessions", methods=["GET"])
def active_sessions():
    try:
        sessions = clipplexAPI.PlexSessions().list_video_sessions()
        return jsonify({"sessions": sessions, "polled_at_ms": int(time.time() * 1000)})
    except Exception as error:
        app.logger.exception("Could not load Plex sessions")
        return jsonify({"message": str(error) or "Could not load Plex sessions."}), 502


@app.route("/api/session-preview", methods=["GET"])
def session_preview():
    session_id = request.args.get("session_id")
    expected_identity = request.args.get("media_identity")
    try:
        if not session_id:
            raise ValueError("Select an active Plex session.")
        if not expected_identity:
            raise ValueError("The selected media identity is required.")
        at_ms = int(request.args.get("at_ms", ""))
        plex = clipplexAPI.PlexInfo(session_id=session_id, inspect_media=False)
        if expected_identity and expected_identity != plex.media_identity:
            raise clipplexAPI.StaleSessionError("The selected Plex player changed videos.")
        image, content_type = plex.preview_image(at_ms)
        response = Response(image, content_type=content_type)
        response.headers["Cache-Control"] = "private, max-age=30"
        return response
    except clipplexAPI.StaleSessionError as error:
        return jsonify({"message": str(error)}), 409
    except (TypeError, ValueError) as error:
        return jsonify({"message": str(error) or "Invalid preview timestamp."}), 400
    except FileNotFoundError as error:
        return jsonify({"message": str(error)}), 404
    except Exception as error:
        app.logger.exception("Session preview failed")
        return jsonify({"message": "The current-frame preview is unavailable."}), 422


@app.route("/api/clips", methods=["GET"])
def created_clips():
    try:
        return jsonify({"clips": clip_library.list_clips(sort=request.args.get("sort", "newest"))})
    except MediaFileError as error:
        return jsonify({"message": error.message}), error.status_code
    except (OSError, ffmpeg.Error, sqlite3.Error):
        app.logger.exception("Could not list clips")
        return jsonify({"message": "The clip library could not be loaded."}), 500


@app.route("/api/clips/metadata", methods=["PATCH"])
def update_clip_metadata():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise MediaFileError("Clip details must be a JSON object.")
        try:
            previous = clip_library.describe_clip(payload.get("file_path"))
        except MediaFileError:
            previous = None
        clip = clip_library.save_clip_metadata(payload.get("file_path"), payload)
        warning = ""
        if previous and clip.get("immich_asset_id") and clip.get("clip_title") != previous.get("clip_title"):
            try:
                uploaders.ImmichUploader()._update_description(clip["immich_asset_id"], clip["clip_title"])
            except uploaders.UploadError as error:
                warning = "Clip title saved, but the Immich description could not be updated: " + error.message
                app.logger.warning(
                    "Clip title saved but Immich description sync failed for asset %s: %s",
                    clip["immich_asset_id"], error.message,
                )
        return jsonify({"result": "success", "clip": clip, "warning": warning or None})
    except MediaFileError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except (OSError, ffmpeg.Error, sqlite3.Error):
        app.logger.exception("Could not save clip metadata")
        return jsonify({"result": "error", "message": "The clip details could not be saved."}), 500


@app.route("/api/clips", methods=["DELETE"])
def delete_clip():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise MediaFileError("The delete request must contain a JSON object.")
        delete_immich_asset = _optional_boolean(payload, "delete_immich_asset")
        if delete_immich_asset:
            clip = clip_library.describe_clip(payload.get("file_path"))
            if settings.get("immich_manage_assets") != "true":
                raise MediaFileError("Enable Manage Immich clips after upload before deleting remote assets.", 400)
            if not clip.get("immich_asset_id"):
                raise MediaFileError("This clip has no associated Immich asset.", 400)
            uploaders.ImmichUploader().delete_asset(clip["immich_asset_id"])
        delete_generated_clip(payload.get("file_path"))
        return jsonify({"result": "success"})
    except (MediaFileError, ValueError) as error:
        return jsonify({"result": "error", "message": getattr(error, "message", str(error))}), getattr(error, "status_code", 400)
    except uploaders.UploadError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except OSError:
        app.logger.exception("Could not delete generated clip")
        return jsonify({"result": "error", "message": "The clip could not be deleted."}), 500


@app.route("/api/clips/thumbnail", methods=["GET"])
def clip_thumbnail():
    try:
        thumbnail_path = clip_library.ensure_thumbnail(request.args.get("file_path"))
        response = send_file(thumbnail_path, mimetype="image/jpeg", conditional=True)
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response
    except MediaFileError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code


@app.route("/api/clips/source-options", methods=["GET"])
def clip_source_options():
    try:
        return jsonify(clip_trims.source_options(request.args.get("file_path")))
    except MediaFileError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except Exception:
        app.logger.exception("Could not inspect original Plex source options")
        return jsonify({"result": "error", "message": "Plex source options could not be loaded."}), 502


@app.route("/api/clip-trims", methods=["POST"])
def create_clip_trim():
    try:
        payload = clip_trims.validate_trim_payload(request.get_json(silent=True))
        job_id = clip_job_manager.enqueue(payload)
        return jsonify({
            "result": "queued", "job_id": job_id, "job_type": "clip_trim",
            "status_url": f"/api/jobs/{job_id}",
        }), 202
    except MediaFileError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except JobQueueFull as error:
        return jsonify({"result": "queue_full", "message": str(error)}), 429


@app.route("/api/clip-extension-previews", methods=["POST"])
def create_extension_preview():
    try:
        payload = clip_trims.validate_extension_preview_payload(request.get_json(silent=True))
        job_id = clip_job_manager.enqueue(payload)
        return jsonify({
            "result": "queued", "job_id": job_id, "job_type": "extension_preview",
            "status_url": f"/api/jobs/{job_id}",
        }), 202
    except MediaFileError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except (TypeError, ValueError) as error:
        return jsonify({"result": "error", "message": str(error)}), 400
    except JobQueueFull as error:
        return jsonify({"result": "queue_full", "message": str(error)}), 429


@app.route("/api/uploaders", methods=["GET"])
def available_uploaders():
    response = jsonify({"uploaders": uploaders.configured_uploaders()})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/settings", methods=["GET"])
def get_settings():
    response = jsonify(settings.ui_settings())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/settings", methods=["PATCH"])
def update_settings():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise settings.SettingsError("The settings request must contain a JSON object.")
        result = settings.update_ui_settings(payload.get("values", {}), payload.get("clear", []))
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response
    except settings.SettingsError as error:
        return jsonify({"message": error.message}), error.status_code
    except sqlite3.Error:
        app.logger.exception("Could not save settings")
        return jsonify({"message": "Settings could not be saved."}), 500


@app.route("/api/settings/tests", methods=["POST"])
def test_settings_service():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise settings.SettingsError("The test request must contain a JSON object.")
        result = settings.test_service(payload.get("service"))
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response
    except settings.SettingsError as error:
        return jsonify({"service": payload.get("service") if isinstance(payload, dict) else None, "ok": False, "message": error.message}), error.status_code


@app.route("/api/uploaders/immich/options", methods=["GET"])
def immich_upload_options():
    try:
        if "immich" not in uploaders.configured_uploader_ids():
            raise uploaders.UploadError("Immich is not configured.", 404)
        response = jsonify(uploaders.ImmichUploader().options())
        response.headers["Cache-Control"] = "no-store"
        return response
    except uploaders.UploadError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code


def _upload_string_list(payload, key):
    values = payload.get(key, [])
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError(f"{key} must be a list containing at most 100 values.")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"Every {key} value must be text.")
        cleaned = value.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _optional_boolean(payload, key):
    if key not in payload:
        return False
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false.")
    return value


@app.route("/api/uploads", methods=["POST"])
def upload_clip():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("The upload request must contain a JSON object.")
        file_path = payload.get("file_path")
        uploader_id = payload.get("uploader")
        if not isinstance(uploader_id, str) or not uploader_id.strip():
            raise ValueError("Select an upload service.")
        new_album_name = payload.get("new_album_name") or ""
        if not isinstance(new_album_name, str) or len(new_album_name.strip()) > 255:
            raise ValueError("The new album name must contain at most 255 characters.")
        result, status_code = uploaders.upload_clip(
            file_path=file_path,
            uploader=uploader_id.strip(),
            tag_ids=_upload_string_list(payload, "tag_ids"),
            tag_names=_upload_string_list(payload, "tag_names"),
            album_ids=_upload_string_list(payload, "album_ids"),
            new_album_name=new_album_name.strip(),
            apply_auto_tags=_optional_boolean(payload, "apply_auto_tags"),
        )
        if result.get("asset_id") and uploader_id.strip() == "immich":
            try:
                clip_library.update_clip_fields(file_path, {"immich_asset_id": result["asset_id"]})
            except MediaFileError:
                pass
        return jsonify(result), status_code
    except ValueError as error:
        return jsonify({"result": "error", "message": str(error)}), 400
    except uploaders.UploadError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code


@app.route("/api/immich/assets/check", methods=["POST"])
def check_immich_asset():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise MediaFileError("The Immich asset check must contain a JSON object.")
        file_path = payload.get("file_path")
        clip = clip_library.describe_clip(file_path)
        asset_id = clip.get("immich_asset_id")
        if not asset_id:
            raise MediaFileError("This clip is not linked to an Immich asset.", 404)
        uploader = uploaders.ImmichUploader()
        if not uploader.asset_exists(asset_id):
            clip_library.update_clip_fields(file_path, {"immich_asset_id": ""})
            return jsonify({
                "result": "not_found",
                "exists": False,
                "message": "The linked Immich asset has been deleted",
            }), 404
        response = jsonify({
            "result": "success",
            "exists": True,
            "url": uploader.asset_url(asset_id),
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except MediaFileError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except uploaders.UploadError as error:
        return jsonify({
            "result": "error",
            "message": "The Immich asset could not be verified: " + error.message,
        }), error.status_code
    except sqlite3.Error:
        app.logger.exception("Could not clear a stale Immich asset link")
        return jsonify({"result": "error", "message": "The stale Immich link could not be removed."}), 500


def _auto_upload_immich(clip, progress=None):
    if settings.get("immich_auto_upload") != "true" or "immich" not in uploaders.configured_uploader_ids():
        return None
    if progress:
        progress("uploading", 99, 0, "Uploading to Immich.")
    result, _ = uploaders.upload_clip(clip["file_path"], "immich", apply_auto_tags=True)
    if result.get("asset_id"):
        clip_library.update_clip_fields(clip["file_path"], {"immich_asset_id": result["asset_id"]})
        clip = clip_library.describe_clip(clip["file_path"])
    if progress:
        progress("uploading", 99, 100, "Immich upload finished.")
    return {"clip": clip, "upload": result}


@app.route("/api/immich/uploads/missing", methods=["POST"])
def upload_missing_immich_clips():
    try:
        if "immich" not in uploaders.configured_uploader_ids():
            raise uploaders.UploadError("Immich is not configured.", 400)
        paths = [clip["file_path"] for clip in clip_library.list_clips() if not clip.get("immich_asset_id")]
        job_id = clip_job_manager.enqueue({"job_type": "immich_bulk_upload", "file_paths": paths})
        return jsonify({"result": "queued", "job_id": job_id, "job_type": "immich_bulk_upload", "status_url": f"/api/jobs/{job_id}"}), 202
    except JobQueueFull as error:
        return jsonify({"result": "queue_full", "message": str(error)}), 429
    except uploaders.UploadError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code


@app.route("/api/jobs/<job_id>", methods=["GET"])
@app.route("/api/clip-jobs/<job_id>", methods=["GET"])
def media_job_status(job_id):
    job = clip_job_manager.get(job_id)
    if job is None:
        return jsonify({"message": "This media job is no longer available."}), 404
    response = jsonify(job)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/gif-exports", methods=["POST"])
def create_gif_export():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise gif_exports.GifExportError("The GIF export request must contain a JSON object.", 400)
        file_path = payload.get("file_path")
        cached = gif_exports.cached_export(file_path)
        if cached is not None:
            return jsonify({"result": "success", "export": cached}), 200
        job_id = clip_job_manager.enqueue({
            "job_type": "gif_export",
            "file_path": file_path,
        })
        return jsonify({
            "result": "queued",
            "job_id": job_id,
            "job_type": "gif_export",
            "status_url": f"/api/jobs/{job_id}",
        }), 202
    except gif_exports.GifExportError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code
    except JobQueueFull as error:
        return jsonify({"result": "queue_full", "message": str(error)}), 429

@app.route("/instant_video.html", methods=["GET"])
def timed_video():
    form = formVideo()
    return render_template(
        "instant_video.html",
        form=form,
        title="Make Clip",
        videos=clipplexAPI.Utils.get_videos_in_folder()[:1],
        active_page="make_clip",
    )


@app.route("/clip_library.html", methods=["GET"])
def clip_library_page():
    sort = request.args.get("sort", "newest")
    if sort not in clip_library.SORT_ORDERS:
        sort = "newest"
    return render_template(
        "clip_library.html",
        title="Clip Library",
        clips=clip_library.list_clips(sort=sort),
        selected_sort=sort,
        active_page="clip_library",
    )


@app.route("/settings.html", methods=["GET"])
def settings_page():
    return render_template("settings.html", title="Settings", active_page="settings")

@app.route("/create_video", methods=["POST"])
def create_video():
    try:
        if request.is_json:
            payload = request.get_json(silent=True)
            if payload is None:
                raise ValueError("The clip request must contain a valid JSON object.")
            queued_payload = validate_json_clip_payload(payload)
            job_id = clip_job_manager.enqueue(queued_payload)
            return jsonify({
                "result": "queued",
                "job_id": job_id,
                "job_type": "clip",
                "status_url": f"/api/jobs/{job_id}",
            }), 202
        else:
            args = request.args
            _pad_time = clipplexAPI.Utils()._pad_time
            start = f"{_pad_time(args.get('start_hour'))}:{_pad_time(args.get('start_minute'))}:{_pad_time(args.get('start_second'))}"
            end = f"{_pad_time(args.get('end_hour'))}:{_pad_time(args.get('end_minute'))}:{_pad_time(args.get('end_second'))}"
            result = get_instant_video(
                username=args.get('username'),
                start=start,
                end=end,
                audio_stream_id=args.get('audio_stream_id'),
                subtitle_stream_id=args.get('subtitle_stream_id'),
                expected_media_identity=args.get('expected_media_identity'),
                expected_session_id=args.get('expected_session_id'),
            )
        return jsonify(result)
    except clipplexAPI.TrackSelectionError as error:
        plex = error.plex_data
        return jsonify({
            "result": "track_selection_required",
            "message": error.message,
            "media_identity": plex.media_identity,
            "session_id": plex.session_identifier,
            "failed_track_id": error.failed_track.id if error.failed_track else None,
            "tracks": plex.track_options(),
        }), 422
    except clipplexAPI.StaleSessionError as error:
        return jsonify({"result": "stale_session", "message": str(error)}), 409
    except clipplexAPI.UnsupportedVideoError as error:
        return jsonify({"result": "unsupported_video", "message": str(error)}), 422
    except clipplexAPI.VideoConversionError as error:
        return jsonify({"result": "video_conversion_failed", "message": str(error)}), 422
    except ValueError as error:
        return jsonify({"result": "error", "message": str(error)}), 400
    except JobQueueFull as error:
        return jsonify({"result": "queue_full", "message": str(error)}), 429
    except Exception as error:
        app.logger.exception("Video creation failed")
        return jsonify({
            "result": "error",
            "message": str(error) or "Clipplex could not create the clip.",
        }), 500


def validate_json_clip_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("The clip request must be a JSON object.")
    if not payload.get("session_id"):
        raise ValueError("Select an active Plex session.")
    if not payload.get("media_identity"):
        raise ValueError("The selected media identity is required.")
    try:
        start_ms = int(payload.get("start_ms"))
        end_ms = int(payload.get("end_ms"))
    except (TypeError, ValueError) as error:
        raise ValueError("Start and End must be valid millisecond timestamps.") from error
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("The clip end time must be later than its start time.")
    return {
        "session_id": str(payload["session_id"]),
        "media_identity": str(payload["media_identity"]),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "audio_stream_id": payload.get("audio_stream_id"),
        "subtitle_stream_id": payload.get("subtitle_stream_id"),
    }

def get_instant_video(
    username=None,
    start=None,
    end=None,
    session_id=None,
    start_ms=None,
    end_ms=None,
    audio_stream_id=None,
    subtitle_stream_id=None,
    expected_media_identity=None,
    expected_session_id=None,
    progress_callback=None,
):
    def progress(stage, overall, stage_progress, message):
        if progress_callback is not None:
            progress_callback(stage, overall, stage_progress, message)

    request_started_at = time.monotonic()
    plex_started_at = time.monotonic()
    progress("validating", 2, 0, "Checking the selected Plex session.")
    plex_data = clipplexAPI.PlexInfo(username=username, session_id=session_id)
    progress("validating", 8, 75, "Validating media identity and selected tracks.")
    plex_elapsed = time.monotonic() - plex_started_at
    log_plex = app.logger.warning if plex_elapsed > 5.0 else app.logger.info
    log_plex("Plex session and media inspection finished in %.2fs", plex_elapsed)
    if expected_media_identity and expected_media_identity != plex_data.media_identity:
        raise clipplexAPI.StaleSessionError(
            "The playing video changed. Check the current stream before retrying."
        )
    if expected_session_id and expected_session_id != plex_data.session_identifier:
        raise clipplexAPI.StaleSessionError(
            "The Plex playback session changed. Check the current stream before retrying."
        )
    if start_ms is None or end_ms is None:
        start_ms = clipplexAPI.Utils.time_to_milliseconds(start)
        end_ms = clipplexAPI.Utils.time_to_milliseconds(end)
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if start_ms < 0:
        raise ValueError("The clip start time cannot be negative.")
    if plex_data.duration_ms and end_ms > plex_data.duration_ms:
        raise ValueError("The clip end time is past the end of the playing video.")
    clip_duration_ms = end_ms - start_ms
    if clip_duration_ms <= 0:
        raise ValueError("The clip end time must be later than its start time.")
    clip_time = clip_duration_ms / 1000.0
    audio_track, subtitle_track = plex_data.resolve_tracks(audio_stream_id, subtitle_stream_id)
    progress("validating", 10, 100, "The clip range and selected tracks are ready.")
    media_name = plex_data.media_title.replace(" ", "")
    file_name = f"{plex_data.username}_{media_name}_{int(time.time())}"
    print(
        f"Creating video of {clip_time} seconds starting at {start_ms}ms "
        f"for session {plex_data.session_identifier}"
    )
    video = clipplexAPI.Video(
        plex_data,
        start_ms,
        clip_time,
        file_name,
        audio_track,
        subtitle_track,
    )
    source_metadata = {
        "media_library": video.metadata_library,
        "media_type": video.metadata_media_type,
        "title": video.metadata_title,
        "show": video.metadata_showname,
        "season_number": video.metadata_season,
        "episode_number": video.metadata_episode_number,
        "year": video.metadata_year,
        "username": plex_data.username,
    }
    clip_identity = clip_library.allocate_clip_title(source_metadata)
    source_provenance = clip_trims.build_source_provenance(plex_data, audio_track, subtitle_track)
    video.metadata_clip_title = clip_identity["clip_title"]
    video.extract_video(progress)
    progress("finalizing", 99, 90, "Reading the completed clip metadata.")
    try:
        clip = clip_library.save_clip_metadata(video.output_path, {
            **source_metadata,
            **clip_identity,
            "original_start_time": video.metadata_current_media_time,
            "original_end_time": video.metadata_end_time,
            "source": source_provenance,
        }, initialize=True)
        try:
            clip_library.ensure_thumbnail(clip["file_path"])
        except (MediaFileError, OSError):
            app.logger.warning("Could not create the new clip thumbnail", exc_info=True)
    except (MediaFileError, OSError, ffmpeg.Error):
        app.logger.warning("Could not save the new clip library metadata", exc_info=True)
        clip = clipplexAPI.Utils.get_video_in_folder(video.output_path)
    auto_upload = None
    try:
        auto_upload = _auto_upload_immich(clip, progress)
        if auto_upload:
            clip = auto_upload["clip"]
    except uploaders.UploadError as error:
        auto_upload = {"warning": error.message}
    request_elapsed = time.monotonic() - request_started_at
    log_request = app.logger.warning if request_elapsed > 5.0 else app.logger.info
    log_request(
        "Create-video request finished in %.2fs",
        request_elapsed,
    )
    return {
        "result": "success",
        "clip": clip,
        "immich_auto_upload": auto_upload,
    }


def run_clip_job(payload, progress):
    try:
        return get_instant_video(
            session_id=payload["session_id"],
            start_ms=payload["start_ms"],
            end_ms=payload["end_ms"],
            audio_stream_id=payload.get("audio_stream_id"),
            subtitle_stream_id=payload.get("subtitle_stream_id"),
            expected_media_identity=payload["media_identity"],
            expected_session_id=payload["session_id"],
            progress_callback=progress,
        )
    except clipplexAPI.TrackSelectionError as error:
        plex = error.plex_data
        raise JobFailure("recovery_required", {
            "result": "track_selection_required",
            "message": error.message,
            "media_identity": plex.media_identity,
            "session_id": plex.session_identifier,
            "failed_track_id": error.failed_track.id if error.failed_track else None,
            "tracks": plex.track_options(),
            "retry_payload": payload,
        }) from error
    except clipplexAPI.StaleSessionError as error:
        raise JobFailure("failed", {
            "result": "stale_session",
            "message": str(error),
        }) from error
    except clipplexAPI.UnsupportedVideoError as error:
        raise JobFailure("failed", {
            "result": "unsupported_video",
            "message": str(error),
        }) from error
    except clipplexAPI.VideoConversionError as error:
        raise JobFailure("failed", {
            "result": "video_conversion_failed",
            "message": str(error),
        }) from error
    except ValueError as error:
        raise JobFailure("failed", {"result": "error", "message": str(error)}) from error
    except Exception as error:
        app.logger.exception("Video creation job failed")
        raise JobFailure("failed", {
            "result": "error",
            "message": str(error) or "Clipplex could not create the clip.",
        }) from error


def run_media_job(payload, progress):
    if payload.get("job_type") == "immich_bulk_upload":
        failures, warnings, completed = [], [], 0
        paths = payload.get("file_paths") or []
        for index, file_path in enumerate(paths):
            progress("uploading", 5 + (90 * index / max(1, len(paths))), 100 * index / max(1, len(paths)), "Uploading clips to Immich.")
            try:
                result, _ = uploaders.upload_clip(file_path, "immich", apply_auto_tags=True)
                if result.get("asset_id"):
                    clip_library.update_clip_fields(file_path, {"immich_asset_id": result["asset_id"]})
                completed += 1
                if result.get("result") == "partial_success":
                    warnings.append({"file_path": file_path, "message": "; ".join(item["message"] for item in result.get("failures", []))})
            except (uploaders.UploadError, MediaFileError, OSError, sqlite3.Error) as error:
                failures.append({"file_path": file_path, "message": getattr(error, "message", str(error))})
        return {
            "result": "partial_success" if failures or warnings else "success",
            "completed": completed,
            "failed": len(failures),
            "failures": failures,
            "warnings": warnings,
        }
    if payload.get("job_type") == "gif_export":
        try:
            return gif_exports.export_gif(payload.get("file_path"), progress)
        except gif_exports.GifExportError as error:
            raise JobFailure("failed", {
                "result": "gif_export_failed",
                "message": error.message,
            }) from error
    if payload.get("job_type") == "clip_trim":
        try:
            result = clip_trims.run_trim_job(payload, progress)
            try:
                auto_upload = None if result.pop("immich_auto_upload_handled", False) else _auto_upload_immich(result["clip"], progress)
            except uploaders.UploadError as error:
                auto_upload = {"warning": error.message}
            if auto_upload:
                result["clip"] = auto_upload["clip"]
                result["immich_auto_upload"] = auto_upload
            return result
        except clip_trims.ClipTrimError as error:
            raise JobFailure("failed", {
                "result": "clip_trim_failed", "message": error.message,
                "status_code": error.status_code,
            }) from error
    if payload.get("job_type") == "extension_preview":
        try:
            return clip_trims.run_extension_preview_job(payload, progress)
        except clip_trims.ClipTrimError as error:
            raise JobFailure("failed", {
                "result": "extension_preview_failed", "message": error.message,
                "status_code": error.status_code,
            }) from error
    return run_clip_job(payload, progress)


clip_job_manager = ClipJobManager(run_media_job)

@app.route("/quick_add_time_to_start_time", methods=["POST"])
def quick_add_time_to_start_time():
    start_time = request.args.get("start_time")
    time_to_add = int(request.args.get("time_to_add"))
    return clipplexAPI.Utils().add_time(start_time, time_to_add)

@app.route("/remove_file", methods=["POST"])
#@login_required
def remove_file():
    video_path = request.args.get("file_path")
    if clipplexAPI.Utils().delete_file(video_path):
        return redirect("/instant_video.html")
    else:
        return "Problem downloading the file"

@app.route("/streamable_upload", methods=["POST"])
def streamable_upload():
    try:
        result, status_code = uploaders.upload_clip(
            file_path=request.args.get("file_path"),
            uploader="streamable",
        )
        return jsonify(result), status_code
    except uploaders.UploadError as error:
        return jsonify({"result": "error", "message": error.message}), error.status_code

@app.route("/login.html", methods=["GET", "POST"])
def login():
    return render_template("login.html")

@app.route("/signin", methods=["POST"])
def signin():
    token = request.get_json()['token']
    valid_login, user_details, user_group = check_credentials(token=token)
    print(valid_login, user_details, user_group)

def check_credentials(token=None):
    """Verifies credentials for username and password.
    Returns True and the user group on success or False and no user group"""
    plex_login = plex_user_login(token=token)
    
    if plex_login is not None:
        return True, plex_login[0], plex_login[1]
