from app import app
from app import clip_library
from app.settings import require_plex_settings


require_plex_settings()
clip_library.sync_library()
