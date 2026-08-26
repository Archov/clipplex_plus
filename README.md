# Clipplex

Have you ever, while watching something on your plex server, wanted to easily extract a clip out of a good movie or tv show you're watching to share it with your friend, family or the world? While this was always possible, the process can be complex for something "so simple".

![](https://github.com/jo-nike/clipplex/blob/master/example.gif)

## Description

An in-depth paragraph about your project and overview of use.

## Docker deployment

Copy `.env.sample` to `.env`, then configure these values:

| Variable | Example | Requirement |
| --- | --- | --- |
| `MEDIA_PATH` | `/mnt/media` | Required host path containing Plex media |
| `CLIP_PATH` | `/mnt/media-clips` | Required host path where generated clips are stored |
| `PLEX_URL` | `http://plex:32400` | Required Plex server URL |
| `PLEX_TOKEN` | `...` | Required Plex authentication token |
| `PUID` | `1000` | Optional runtime UID; defaults to 1000 |
| `PGID` | `1000` | Optional runtime GID; defaults to 1000 |
| `TZ` | `America/Chicago` | Optional container timezone |
| `STREAMABLE_LOGIN` | `...` | Optional; Streamable requires both login and password |
| `STREAMABLE_PASSWORD` | `...` | Optional; Streamable requires both login and password |
| `IMMICH_URL` | `http://immich-server:2283` | Optional; Immich requires both URL and API key |
| `IMMICH_API_KEY` | `...` | Optional; see the exact permissions below |
| `IMMICH_DEFAULT_TAG` | `#plex-clip` | Optional tag applied to every Immich upload |
| `FFMPEG_PRESET` | `veryfast` | Optional x264 speed/compression tradeoff |

`MEDIA_PATH` is mounted read-only at `/data/media`. Plex's reported media paths must be reachable at the same absolute paths inside Clipplex; adjust the Compose mount target if Plex uses a path other than `/data/media`.

`CLIP_PATH` is mounted read/write at `/app/app/static/media`. The host directory or network share must allow the configured `PUID` and `PGID` to create files and directories. The container runs without root privileges.

### Immich API key permissions

Clipplex targets Immich v1.135 or newer for creating a restricted API key through the Immich web interface. For all Clipplex Immich upload features, give the key only these permissions:

```text
asset.upload
tag.read
tag.create
tag.asset
album.read
album.create
albumAsset.create
```

These permissions allow Clipplex to upload the video (`asset.upload`), list, create, and assign tags (`tag.read`, `tag.create`, and `tag.asset`), and list, create, and add assets to albums (`album.read`, `album.create`, and `albumAsset.create`). The `all` permission and asset read, update, delete, download, or sharing permissions are not required on supported Immich versions.

Finding Plex token: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

### Build and start

```sh
cp .env.sample .env
# Edit .env before starting the service.
docker compose up -d --build
```

Open `http://<docker-host>:9945`. Clipplex must be able to reach Plex over the configured network. View logs with:

```sh
docker compose logs -f clipplex
```

For a direct `docker run` deployment, build the image locally and pass `--user UID:GID`, runtime credentials, the read-only media mount, and the writable clips mount. Credentials must be supplied when the container starts, never as Docker build arguments.

### Process model

Clip and GIF jobs and their progress are stored in memory and processed by one worker thread. Run exactly one Clipplex application process. Do not configure multiple Gunicorn workers until the job queue and job state use shared storage.

## Exporting GIFs

Every saved clip has an **Export GIF** action. Clipplex creates a silent, looping GIF and downloads it so it can be attached to services such as Discord or Facebook. GIF export runs in the same background queue as clip creation, so only one FFmpeg render runs at a time.

Exports are automatically reduced through several resolution, frame-rate, and color profiles until they fit below 9.5 MB. If a long or visually complex clip cannot meet that limit, create a shorter clip and try again. GIF does not support audio.

Successful exports are cached under `static/media/gifs` within the configured `CLIP_PATH` mount. Re-exporting an unchanged clip reuses the cached file. Deleting the MP4 clip also deletes its cached GIF; GIFs do not appear as separate items in the clip library.

## Authors

Contributors names and contact info

Jo Nike

## Version History

* 0.0.3
    
    Initial Release

## License

Distributed under the MIT License. See the LICENSE file information.

## Acknowledgments

* Thanks to the resident of flavourtown for allowing me to pitch my ideas and share my progress with them.

* Thanks to [Start Bootstrap](https://github.com/startbootstrap/startbootstrap-sb-admin) for the UI.
