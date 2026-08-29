from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigurationTests(unittest.TestCase):
    def test_docker_image_does_not_bake_runtime_credentials(self):
        dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertNotIn("ARG PLEX_TOKEN", dockerfile)
        self.assertNotIn("ENV PLEX_TOKEN", dockerfile)
        self.assertNotIn("ARG STREAMABLE_PASSWORD", dockerfile)
        self.assertNotIn("ENV STREAMABLE_PASSWORD", dockerfile)
        self.assertIn("USER clipplex:clipplex", dockerfile)
        self.assertIn("/app/app/static/media/gifs", dockerfile)

    def test_docker_image_uses_single_process_waitress_with_four_threads(self):
        dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("waitress==3.0.2", requirements)
        self.assertIn(
            'CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=4", "main:app"]',
            dockerfile,
        )
        self.assertNotIn('"flask", "run"', dockerfile)
        self.assertNotIn("ENV FLASK_APP", dockerfile)

    def test_windows_startup_synchronizes_all_dependencies_once(self):
        script = (REPOSITORY_ROOT / "run.ps1").read_text(encoding="utf-8")

        self.assertEqual(script.count("-m pip install -r requirements.txt"), 1)
        self.assertEqual(script.count('-c "import waitress"'), 1)
        self.assertIn(
            '}\n\nWrite-Host "Installing Clipplex dependencies..."\n'
            '& $Python -m pip install -r requirements.txt',
            script,
        )

    def test_wsgi_startup_validates_settings_and_synchronizes_storage(self):
        main = (REPOSITORY_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("require_plex_settings()", main)
        self.assertIn("clip_library.sync_library()", main)

    def test_compose_maps_the_configured_runtime_identity(self):
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        environment_sample = (REPOSITORY_ROOT / ".env.sample").read_text(encoding="utf-8")

        self.assertNotIn("FLASK_APP", compose)
        self.assertIn('user: "${PUID:-1000}:${PGID:-1000}"', compose)
        self.assertIn("PUID=1000", environment_sample)
        self.assertIn("PGID=1000", environment_sample)
        self.assertIn("TZ=America/Chicago", environment_sample)
        self.assertIn("FFMPEG_PRESET=", environment_sample)
        self.assertNotIn("ENV FFMPEG_PRESET", (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8"))
        self.assertIn("${MEDIA_PATH}:/data/media:ro", compose)
        self.assertIn("${CLIP_PATH}:/app/app/static/media", compose)

    def test_docker_context_excludes_secrets_and_generated_media(self):
        dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn(".env.*", dockerignore)
        self.assertIn("!.env.sample", dockerignore)
        self.assertIn("app/static/media/*", dockerignore)

    def test_readme_matches_the_compose_deployment_contract(self):
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("`MEDIA_PATH`", readme)
        self.assertIn("`CLIP_PATH`", readme)
        self.assertIn("`PGID`", readme)
        self.assertNotIn("`GUID`", readme)
        self.assertIn("/data/media", readme)
        self.assertIn("docker compose up -d --build", readme)
        self.assertIn("Immich v1.135 or newer", readme)
        self.assertIn("asset.update", readme)
        self.assertIn("asset.read", readme)
        self.assertIn("asset.delete", readme)
        self.assertIn("Manage Immich clips after upload", readme)
        self.assertIn("Run exactly one Clipplex application process", readme)
        self.assertIn("served by Waitress as one application process with four request threads", readme)
        self.assertIn("trusted LAN or VPN", readme)
        self.assertIn("Export GIF", readme)
        self.assertIn("below 9.5 MB", readme)
        self.assertIn("clipplex.sqlite3", readme)
        self.assertIn("may be removed from `.env`", readme)


if __name__ == "__main__":
    unittest.main()
