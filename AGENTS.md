# AGENTS.md

## Project

Clipplex Plus is a Python/Flask web application for creating clips, snapshots,
GIFs, and other derived media from content currently being played through Plex.

The application also manages generated clip metadata, persistent settings,
background media jobs, and optional uploads to external services.

When working in this repository, prefer small, targeted changes that preserve
existing behavior unless the task explicitly calls for a larger redesign.

---

## Runtime and dependencies

Primary runtime:

- Python 3.11
- Flask
- Waitress
- SQLite
- FFmpeg / ffprobe
- ffmpeg-python
- requests

Python dependencies are defined in:

    requirements.txt

Do not add, remove, or upgrade dependencies unless they are required for the
task. If dependencies change, update `requirements.txt`.

FFmpeg and ffprobe are external runtime dependencies and are expected to be
available on PATH.

---

## Repository structure

Important files and directories:

    main.py
        WSGI application entry point.
    
    clipplexAPI.py
        Core Plex/media integration and related media-processing logic.
    
    app/
        Flask application package.
    
    app/__init__.py
        Flask initialization, persistent settings initialization, and media
        directory setup.
    
    app/routes.py
        HTTP routes and API endpoints.
    
    app/jobs.py
        Background job queue and job state.
    
    app/clip_library.py
        Generated clip library and metadata handling.
    
    app/clip_trims.py
        Clip trimming/export logic.
    
    app/gif_exports.py
        GIF export logic.
    
    app/uploaders.py
        External uploader integrations.
    
    app/database.py
        SQLite connection, schema, and migration logic.
    
    app/settings.py
        Persistent application settings.
    
    app/media_files.py
        Generated-media filesystem operations.
    
    app/templates/
        Jinja templates.
    
    app/static/
        CSS, JavaScript, images, and generated media paths.
    
    tests/
        unittest test suite.
    
    run.ps1
        Windows development launcher.
    
    Dockerfile
    docker-compose.yml
        Container deployment.
    
    .github/workflows/
        GitHub Actions workflows.

Before modifying unfamiliar functionality, inspect the corresponding module and
its tests.

---

## Development setup

On Windows, the preferred local startup path is:

    .\run.ps1

`run.ps1`:

- loads `.env` when present
- creates a Python 3.11 virtual environment if needed
- installs `requirements.txt`
- verifies FFmpeg and ffprobe
- starts Waitress on port 9945

The application should then be available at:

    http://localhost:9945

For container testing:

    docker compose up -d --build

Do not commit `.env`, credentials, generated media, SQLite databases, or other
runtime data.

---

## Tests

Tests use Python's built-in `unittest` framework.

Tests should focus on scenarios that have a real chance to catch actual regressions/bugs rather than fragile, superficial, or aesthetic validations.

Run the full suite with:

    .\.venv\Scripts\python.exe -m unittest discover -s tests -v

or, when using another configured Python environment:

    python -m unittest discover -s tests -v

Run focused tests while developing when useful, for example:

    python -m unittest tests.test_jobs -v
    python -m unittest tests.test_database -v
    python -m unittest tests.test_routes -v

Before considering a code change complete:

1. Run tests directly related to the changed code.
2. Run the complete test suite when practical.
3. Add or update tests when behavior changes or a bug is fixed.

Do not change tests merely to make an incorrect implementation pass.

---

## Change policy

Prefer:

- minimal diffs
- existing project conventions
- straightforward Python over unnecessary abstraction
- extending existing modules before adding new architectural layers
- explicit failure handling
- useful logging for failures involving Plex, FFmpeg, filesystems, databases,
  or external services

Avoid:

- unrelated cleanup in the same change
- broad rewrites without a clear requirement
- speculative abstractions
- silently changing existing API behavior
- silently changing filenames or generated-media layout
- swallowing exceptions without a deliberate reason
- adding dependencies for functionality that can reasonably use the standard
  library or existing dependencies

When fixing a bug, identify the underlying cause rather than only masking its
visible symptom.

---

## Application architecture constraints

### Single-process server

Clipplex currently runs as exactly one Waitress application process with four
request threads.

The background job queue and job progress state are stored in application
memory.

Do NOT:

- add multiple Waitress process workers
- deploy through a multi-process WSGI configuration
- assume job state can be shared between processes

unless the job queue/state architecture is first changed to use shared
persistent storage.

Thread-safety still matters because HTTP requests and the background worker may
operate concurrently.

---

## Background jobs

Long-running FFmpeg work should not block Flask request handlers when it can be
handled by the existing job infrastructure.

When modifying job behavior:

- preserve useful progress reporting
- preserve deterministic job state transitions
- handle worker exceptions cleanly
- clean up temporary files after failure when appropriate
- avoid starting multiple competing FFmpeg operations unless concurrency is an
  intentional design change
- consider cancellation, failure, and partial-output cases

Changes to `app/jobs.py` should normally include tests in `tests/test_jobs.py`
or another directly relevant test module.

---

## FFmpeg and media handling

Media files may be large. Avoid reading entire video files into memory.

Prefer streaming, filesystem paths, FFmpeg operations, and bounded buffers.

When constructing FFmpeg operations:

- preserve requested audio/subtitle behavior
- verify stream indexes before relying on them
- handle filenames containing spaces and unusual characters safely
- do not construct shell commands through unsafe string concatenation
- clean up incomplete temporary outputs
- avoid unnecessary transcoding when stream copy is appropriate

Do not assume all Plex media uses:

- H.264
- AAC
- one audio stream
- no subtitles
- CFR video
- 16:9 aspect ratio
- simple ASCII filenames

Use ffprobe/media metadata when the information is required.

---

## Plex integration

Plex data may change between the time a page is loaded and an operation is
executed.

Code dealing with active playback sessions should account for:

- sessions disappearing
- playback changing to another media item
- stale media identity
- missing media files
- Plex being unreachable
- malformed or incomplete Plex responses

Do not weaken stale-session or media-identity checks merely to make an operation
succeed.

Network failures should produce useful application errors rather than crashes
where practical.

---

## Filesystem safety

The Plex source media library should be treated as read-only.

Generated Clipplex media lives beneath the configured clip/media directory.

Never delete or modify the original Plex source file.

Be particularly careful with:

- path normalization
- user-controlled filenames
- directory traversal
- recursive deletion
- symlinks
- temporary files
- cleanup after failed FFmpeg jobs

Deletion functions should operate only on files Clipplex is explicitly expected
to manage.

The private `.clipplex` directory beneath the media path must never become
publicly accessible through Flask static-file serving.

---

## Database

Clipplex uses SQLite directly through `sqlite3`.

Database schema versioning is managed in:

    app/database.py

Schema changes must:

- increment `SCHEMA_VERSION`
- add an explicit migration
- support existing user databases
- preserve existing data whenever reasonably possible
- update relevant database tests

Do not assume every user starts with a fresh database.

Do not edit existing migrations in a way that changes what an already-released
schema version means. Add a new migration instead.

Use transactions where a partially completed operation would leave inconsistent
state.

Keep SQLite concurrency limitations in mind when code may execute from request
threads and the background worker.

---

## Settings and secrets

Application settings can be persisted in SQLite and may initially be populated
or overridden from environment variables.

Secrets include things such as:

- Plex tokens
- Streamable credentials
- Immich API keys
- Flask secret values

Never:

- hard-code secrets
- commit real credentials
- print secrets to logs
- expose secrets through API responses
- bake runtime credentials into Docker images

Example configuration belongs in `.env.sample`, with placeholder values only.

---

## Docker and deployment

The Docker image is intentionally configured to run as a non-root user.

Do not change the container to run as root merely to work around filesystem
permission problems.

The source-media mount should remain read-only.

If deployment behavior changes, inspect and update together as appropriate:

    Dockerfile
    docker-compose.yml
    .env.sample
    README.md
    tests/test_deployment.py

Deployment documentation and deployment tests are part of the application's
contract.

---

## Web/API changes

For API endpoints:

- preserve existing response shapes unless intentionally changing the API
- use appropriate HTTP status codes
- return actionable error messages
- validate user-controlled input
- avoid leaking filesystem paths, credentials, stack traces, or private
  configuration unnecessarily

For UI changes:

- check both the relevant Jinja template and JavaScript
- preserve existing API compatibility where possible
- avoid introducing frontend frameworks unless explicitly requested
- maintain usable behavior when an operation fails or is still processing
- follow existing graphic design standards (IE:Dark mode)

---

## Testing expectations by area

Typical test mappings:

    app/clip_library.py  -> tests/test_clip_library.py
    app/clip_trims.py    -> tests/test_clip_trims.py
    clipplexAPI.py       -> tests/test_clipplex_api.py
    app/database.py      -> tests/test_database.py
    deployment files     -> tests/test_deployment.py
    app/gif_exports.py   -> tests/test_gif_exports.py
    app/jobs.py          -> tests/test_jobs.py
    app/routes.py        -> tests/test_routes.py
    app/uploaders.py     -> tests/test_uploaders.py

This mapping is guidance, not a restriction. Add tests wherever they best
describe the behavior being protected.

---

## Git and scope

Do not modify unrelated files.

PRs should always target the forked master and never the upstream repo.

Do not commit:

- `.env`
- `.venv`
- SQLite runtime databases
- generated clips
- generated GIFs
- previews
- thumbnails
- temporary FFmpeg files
- credentials or API keys

Keep commits focused on one logical change when practical.

Do not create tags or releases unless explicitly requested.

Container publishing is triggered by version tags matching `v*` or by manual
workflow dispatch. Treat creation of a version tag as a release action, not a
normal development step.

---

## Documentation

Update documentation when a change affects:

- installation
- environment variables
- Docker deployment
- externally visible behavior
- supported uploader configuration
- security expectations
- operational constraints

Prefer documenting the behavior that actually exists rather than planned or
aspirational behavior.

---

## Before finishing a task

Verify:

- the requested behavior is implemented
- relevant tests pass
- new behavior has tests where appropriate
- no unrelated files were changed
- no credentials or runtime files were added
- failure paths were considered
- existing Plex source media cannot be unintentionally modified
- SQLite migrations remain compatible with existing databases
- the single-process job-queue constraint remains valid
- deployment documentation/tests were updated if deployment behavior changed

In the final summary, clearly state:

- what changed
- what tests were run
- any tests that could not be run
- any remaining limitations or follow-up work