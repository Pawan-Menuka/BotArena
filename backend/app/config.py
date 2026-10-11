from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(default_factory=lambda: Path(os.getenv("DATABASE_PATH", "./data/botarena.db")))
    deployment_mode: str = field(default_factory=lambda: os.getenv("DEPLOYMENT_MODE", "local"))

    def __post_init__(self) -> None:
        if self.deployment_mode not in {"local", "shared"}:
            raise ValueError("DEPLOYMENT_MODE must be local or shared")
