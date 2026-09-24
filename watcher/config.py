"""Runtime settings (environment) and static config (YAML files in /config)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TZ = ZoneInfo("Europe/London")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///watcher.db"))
    gmail_address: str = field(default_factory=lambda: _env("GMAIL_ADDRESS"))
    gmail_app_password: str = field(default_factory=lambda: _env("GMAIL_APP_PASSWORD").replace(" ", ""))
    alert_to: str = field(default_factory=lambda: _env("ALERT_TO"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5"))
    llm_monthly_call_cap: int = field(default_factory=lambda: int(_env("LLM_MONTHLY_CALL_CAP", "3000")))
    keepa_api_key: str = field(default_factory=lambda: _env("KEEPA_API_KEY"))
    track_base_url: str = field(default_factory=lambda: _env("TRACK_BASE_URL"))
    link_secret: str = field(default_factory=lambda: _env("LINK_SECRET"))  # else read from the database
    dry_run: bool = field(default_factory=lambda: _env("DRY_RUN", "0") == "1")
    gmail_backfill_days: int = field(default_factory=lambda: int(_env("GMAIL_BACKFILL_DAYS", "365")))

    @property
    def watch_address(self) -> str:
        """Email this address (e.g. you+watch@gmail.com) to add a planned purchase."""
        if "@" not in self.gmail_address:
            return ""
        user, domain = self.gmail_address.split("@", 1)
        return f"{user}+watch@{domain}"

    @property
    def recipient(self) -> str:
        return self.alert_to or self.gmail_address


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def categories() -> dict:
    data = yaml.safe_load((CONFIG_DIR / "categories.yaml").read_text())
    data.pop("sale_events", None)
    return data


@lru_cache
def sale_events() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "categories.yaml").read_text()).get("sale_events", [])


@lru_cache
def retailers() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "retailers.yaml").read_text())


@lru_cache
def sources() -> dict:
    return yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text())


def category_rules(cat: str | None) -> dict:
    cats = categories()
    return cats.get(cat or "other") or cats["other"]
