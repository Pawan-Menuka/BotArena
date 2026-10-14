from __future__ import annotations

import argparse
import os

import uvicorn

from app.config import Settings
from app.engine.provider import FakeProvider, GroqProvider
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description="Run the local BotArena API")
    parser.add_argument("--provider", choices=["fake", "groq"], default="fake")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings()
    app = create_app(settings, provider_factory=FakeProvider if args.provider == "fake" else GroqProvider,
                     model_id="fake" if args.provider == "fake" else os.getenv("GROQ_MODEL", "unconfigured"))
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
