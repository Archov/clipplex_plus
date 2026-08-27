from flask import Flask, abort, request
import os

media_folder = os.path.join(os.path.dirname(__file__), "static", "media")
folders = [
    os.path.join(media_folder, "videos"),
    os.path.join(media_folder, "images"),
    os.path.join(media_folder, "gifs"),
    os.path.join(media_folder, "thumbnails"),
    os.path.join(media_folder, "previews"),
    os.path.join(media_folder, ".clipplex", "work"),
]
for folder in folders:
    os.makedirs(folder, exist_ok=True)

from app import settings

settings.initialize_settings()

app = Flask(__name__, static_url_path="/static")
app.config["SECRET_KEY"] = settings.get("flask_secret_key")


@app.before_request
def block_private_media():
    path = request.path.lower()
    if path.startswith("/static/media/.clipplex/"):
        abort(404)


from app import routes
