"""
Application entrypoint.

    python app.py

Run migrations first (python scripts/migrate.py) if this is a fresh
database. See README.md for the full setup sequence.
"""
import os

from flask import Flask

from src.web.routes import bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("RECON_SECRET_KEY", "dev-secret-key-not-for-production")
    app.register_blueprint(bp)
    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
