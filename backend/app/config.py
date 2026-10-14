from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from ipaddress import ip_network
from decimal import Decimal


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
    global_call_quota_24h: int = field(default_factory=lambda: int(os.getenv("GLOBAL_CALL_QUOTA_24H", "300")))
    max_reserved_cost_24h_usd: Decimal | None = field(default_factory=lambda: (
        Decimal(os.environ["MAX_RESERVED_COST_24H_USD"]) if os.getenv("MAX_RESERVED_COST_24H_USD", "").strip() else None))
    input_accounting_confirmed: bool = field(default_factory=lambda: os.getenv("INPUT_ACCOUNTING_CONFIRMED", "false").lower() == "true")

    def __post_init__(self) -> None:
        if self.deployment_mode not in {"local", "shared"}:
            raise ValueError("DEPLOYMENT_MODE must be local or shared")
        for network in self.trusted_proxy_networks:
            ip_network(network)
        if not self.client_ip_header or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in self.client_ip_header):
            raise ValueError("CLIENT_IP_HEADER must be an HTTP header name")
        for name in ("hmac_key_version", "create_quota_per_ip_24h", "global_create_quota_24h", "concurrency_per_ip", "global_concurrency", "global_call_quota_24h"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.global_concurrency > 2:
            raise ValueError("GLOBAL_CONCURRENCY cannot exceed the single-worker capacity of 2")
        if self.max_reserved_cost_24h_usd is not None:
            value = Decimal(str(self.max_reserved_cost_24h_usd))
            if not value.is_finite() or value <= 0 or value * 100 != (value * 100).to_integral_value():
                raise ValueError("MAX_RESERVED_COST_24H_USD must be a positive amount in whole cents")
            object.__setattr__(self, "max_reserved_cost_24h_usd", value)

    @property
    def cost_cap_cents(self) -> int | None:
        return int(self.max_reserved_cost_24h_usd * 100) if self.max_reserved_cost_24h_usd is not None else None

    def validate_execution_budget(self, model_id: str) -> None:
        from app.engine.budget import price_for
        price_for(model_id)
        if model_id != "fake" and Path(str(self.database_path) + ".restore-pause").exists():
            raise ValueError("Paid execution is paused until post-restore ledger reconciliation")
        if self.deployment_mode == "shared" and self.cost_cap_cents is None:
            raise ValueError("Shared execution requires MAX_RESERVED_COST_24H_USD")
        if model_id != "fake" and not self.input_accounting_confirmed:
            raise ValueError("Provider input accounting must be confirmed before paid execution")

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
