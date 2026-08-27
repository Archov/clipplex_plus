# Clipplex

Have you ever, while watching something on your plex server, wanted to easily extract a clip out of a good movie or tv show you're watching to share it with your friend, family or the world? While this was always possible, the process can be complex for something "so simple".

![](https://github.com/jo-nike/clipplex/blob/master/example.gif)

## Description

An in-depth paragraph about your project and overview of use.

## Docker deployment

Copy `.env.sample` to `.env`, then configure the deployment paths and provide Plex credentials for the first startup:

| Variable | Example | Requirement |
| --- | --- | --- |
| `MEDIA_PATH` | `/mnt/media` | Required host path containing Plex media |
| `CLIP_PATH` | `/mnt/media-clips` | Required host path where generated clips are stored |
| `PLEX_URL` | `http://plex:32400` | Required once; imported into SQLite |
| `PLEX_TOKEN` | `...` | Required once; imported into SQLite |
| `PUID` | `1000` | Optional runtime UID; defaults to 1000 |
| `PGID` | `1000` | Optional runtime GID; defaults to 1000 |
| `TZ` | `America/Chicago` | Optional container timezone |
| `STREAMABLE_LOGIN` | `...` | Optional bootstrap setting; Streamable requires both values |
| `STREAMABLE_PASSWORD` | `...` | Optional bootstrap setting; imported into SQLite |
| `IMMICH_URL` | `http://immich-server:2283` | Optional bootstrap setting; Immich also requires an API key |
| `IMMICH_API_KEY` | `...` | Optional bootstrap setting; see the exact permissions below |
| `IMMICH_DEFAULT_TAG` | `#plex-clip` | Optional bootstrap setting applied to every Immich upload |
| `FFMPEG_PRESET` | `veryfast` | Optional bootstrap setting; defaults to `veryfast` |

`MEDIA_PATH` is mounted read-only at `/data/media`. Plex's reported media paths must be reachable at the same absolute paths inside Clipplex; adjust the Compose mount target if Plex uses a path other than `/data/media`.

`CLIP_PATH` is mounted read/write at `/app/app/static/media`. The host directory or network share must allow the configured `PUID` and `PGID` to create files and directories. The container runs without root privileges.

### Persistent settings and metadata

Clipplex stores application settings, clip metadata, original-source provenance, and cached media analysis in `.clipplex/clipplex.sqlite3` inside `CLIP_PATH`. On startup, every nonblank application setting supplied through the environment is written to the database and overrides its stored value. Blank or missing variables leave the database value unchanged, so credentials may be removed from `.env` after one successful startup. Keep `MEDIA_PATH`, `CLIP_PATH`, `PUID`, `PGID`, and `TZ` in the deployment environment because Docker needs them before Clipplex can open its database.

Existing `.clipplex/metadata/*.json` and adjacent `*.clipplex.json` files are imported automatically. A sidecar is deleted only after its database transaction commits. Malformed sidecars are retained and reported in the log so they can be repaired and retried.

The SQLite file contains credentials in plaintext and must be protected like the old `.env` file. It is kept below the private `.clipplex` path, blocked from HTTP access, and restricted to the application user where the platform supports file permissions. Back up the complete `CLIP_PATH` volume to preserve both clips and their metadata.

### Managing settings in Clipplex

Open **Settings** in the Clipplex sidebar to manage Plex, Streamable, Immich, and FFmpeg configuration after the initial bootstrap. Stored passwords, tokens, and API keys are never shown again; leave a secret field blank to keep it, or use its explicit clear control to remove it. Each service section can test its saved connection without uploading a clip.

While a nonblank bootstrap environment variable is present, it remains authoritative and the matching Settings field is read-only. Remove that variable from `.env` (or the container environment), then recreate or redeploy the Clipplex container before managing that value in the UI. For Compose deployments, run `docker compose up -d`; `docker compose restart` does not reload changed environment values. The generated Flask session secret is intentionally internal and is not exposed in Settings.

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
# Set the deployment paths and initial Plex credentials before the first start.
docker compose up -d --build
```

After the first successful start, remove `PLEX_URL`, `PLEX_TOKEN`, and any uploader credentials from `.env` if you do not want them to override the stored settings on later starts.

Open `http://<docker-host>:9945`. Clipplex must be able to reach Plex over the configured network. View logs with:

```sh
docker compose logs -f clipplex
```

For a direct `docker run` deployment, build the image locally and pass `--user UID:GID`, runtime credentials, the read-only media mount, and the writable clips mount. Credentials must be supplied when the container starts, never as Docker build arguments.

### Process model

Clipplex is served by Waitress as one application process with four request threads. Clip and GIF jobs and their progress are stored in memory and processed by a separate worker thread. Run exactly one Clipplex application process; do not add process workers until the job queue and job state use shared storage.

The published port is intended for a trusted LAN or VPN. Waitress does not provide authentication or HTTPS. Before exposing Clipplex to the internet, put it behind an authenticated HTTPS reverse proxy and configure trusted proxy headers explicitly. This is especially important because the Settings page can change service credentials.

## Exporting GIFs

Every saved clip has an **Export GIF** action. Clipplex creates a silent, looping GIF and downloads it so it can be attached to services such as Discord or Facebook. GIF export runs in the same background queue as clip creation, so only one FFmpeg render runs at a time.

Exports are automatically reduced through several resolution, frame-rate, and color profiles until they fit below 9.5 MB. If a long or visually complex clip cannot meet that limit, create a shorter clip and try again. GIF does not support audio.

Successful exports are cached under `static/media/gifs` within the configured `CLIP_PATH` mount. Re-exporting an unchanged clip reuses the cached file. Deleting the MP4 clip also deletes its cached GIF; GIFs do not appear as separate items in the clip library.

The clip library can sort each media-library group by creation time, title, or duration. Probe-derived durations and embedded metadata are cached in SQLite and refreshed only when a clip's size or modification time changes.

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
