from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from ipaddress import ip_network


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(default_factory=lambda: Path(os.getenv("DATABASE_PATH", "./data/botarena.db")))
    deployment_mode: str = field(default_factory=lambda: os.getenv("DEPLOYMENT_MODE", "local"))
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""), repr=False)
    rate_limit_hmac_secret: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_HMAC_SECRET", ""), repr=False)
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""), repr=False)
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", ""))
    trusted_proxy_networks: tuple[str, ...] = field(default_factory=lambda: tuple(
        item.strip() for item in os.getenv("TRUSTED_PROXY_NETWORKS", "").split(",") if item.strip()))
    client_ip_header: str = field(default_factory=lambda: os.getenv("CLIENT_IP_HEADER", "Fly-Client-IP"))
    hmac_key_version: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_KEY_VERSION", "1")))
    create_quota_per_ip_24h: int = field(default_factory=lambda: int(os.getenv("CREATE_QUOTA_PER_IP_24H", "10")))
    global_create_quota_24h: int = field(default_factory=lambda: int(os.getenv("GLOBAL_CREATE_QUOTA_24H", "100")))
    concurrency_per_ip: int = field(default_factory=lambda: int(os.getenv("CONCURRENCY_PER_IP", "1")))
    global_concurrency: int = field(default_factory=lambda: int(os.getenv("GLOBAL_CONCURRENCY", "2")))

    def __post_init__(self) -> None:
        if self.deployment_mode not in {"local", "shared"}:
            raise ValueError("DEPLOYMENT_MODE must be local or shared")
        for network in self.trusted_proxy_networks:
            ip_network(network)
        if not self.client_ip_header or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in self.client_ip_header):
            raise ValueError("CLIENT_IP_HEADER must be an HTTP header name")
        for name in ("hmac_key_version", "create_quota_per_ip_24h", "global_create_quota_24h", "concurrency_per_ip", "global_concurrency"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.global_concurrency > 2:
            raise ValueError("GLOBAL_CONCURRENCY cannot exceed the single-worker capacity of 2")

    def validate_shared(self) -> None:
        if self.deployment_mode == "local":
            return
        for name, value in (("ADMIN_TOKEN", self.admin_token), ("RATE_LIMIT_HMAC_SECRET", self.rate_limit_hmac_secret),
                            ("GROQ_API_KEY", self.groq_api_key)):
            if not nontrivial(value):
                raise RuntimeError(f"Shared deployment is unavailable: {name} must be a non-trivial secret")
        if (self.groq_model.lower().strip() in {"", "fake", "test", "dummy"}
                or any(word in self.groq_model.lower() for word in ("placeholder", "your-model", "changeme"))):
            raise RuntimeError("Shared deployment is unavailable: GROQ_MODEL must be configured")


def nontrivial(value: str) -> bool:
    return (len(value.strip()) >= 32 and len(set(value)) >= 8
            and not any(word in value.lower() for word in ("changeme", "change-me", "placeholder", "your-key", "your_key", "example", "replace-me", "replace_me")))
