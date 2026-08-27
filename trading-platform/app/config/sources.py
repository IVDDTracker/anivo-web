"""Loaders for YAML source configuration (telegram, github, rss)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.core.logging import get_logger

log = get_logger(__name__)


class TelegramSourceCfg(BaseModel):
    name: str
    identifier: str  # chat id the bot has been explicitly added to
    category: str = "community"
    enabled: bool = True
    reliability_score: float = Field(default=0.2, ge=0.0, le=1.0)
    symbols: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class GitHubSourceCfg(BaseModel):
    name: str
    repo: str  # owner/repo
    assets: list[str] = Field(default_factory=list)
    enabled: bool = True
    reliability_score: float = Field(default=0.6, ge=0.0, le=1.0)
    watch: list[str] = Field(default=["releases", "commits", "advisories"])


class RssSourceCfg(BaseModel):
    name: str
    url: str
    category: str = "news"
    enabled: bool = True
    reliability_score: float = Field(default=0.5, ge=0.0, le=1.0)
    assets: list[str] = Field(default_factory=list)


def _load_yaml_list(path: Path, key: str) -> list[dict]:
    if not path.exists():
        log.warning("source config missing: %s", path)
        return []
    data = yaml.safe_load(path.read_text()) or {}
    items = data.get(key, [])
    return items if isinstance(items, list) else []


def load_telegram_sources(config_dir: Path) -> list[TelegramSourceCfg]:
    return [TelegramSourceCfg(**it) for it in _load_yaml_list(config_dir / "telegram_sources.yaml", "sources")]


def load_github_sources(config_dir: Path) -> list[GitHubSourceCfg]:
    return [GitHubSourceCfg(**it) for it in _load_yaml_list(config_dir / "github_sources.yaml", "sources")]


def load_rss_sources(config_dir: Path) -> list[RssSourceCfg]:
    return [RssSourceCfg(**it) for it in _load_yaml_list(config_dir / "rss_sources.yaml", "sources")]
