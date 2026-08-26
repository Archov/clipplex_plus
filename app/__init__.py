from flask import Flask
import os

media_folder = os.path.join(os.path.dirname(__file__), "static", "media")
folders = [
    os.path.join(media_folder, "videos"),
    os.path.join(media_folder, "images"),
    os.path.join(media_folder, "gifs"),
]
for folder in folders:
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__, static_url_path="/static")
app.config["SECRET_KEY"] = "fdsfsdfasdg34"
from app import routes
