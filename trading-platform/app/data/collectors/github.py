"""GitHub REST collector (official api.github.com, ETag conditional polling).

Watches configured repos for releases, security advisories, notable commits,
archival, and commit-rate anomalies. GitHub activity is CONTEXT, not a trade
trigger, and "more commits" is explicitly not treated as bullish: sentiment
stays neutral except for security advisories (negative).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from app.config.sources import GitHubSourceCfg
from app.core.bus import EventBus, Topics
from app.core.logging import get_logger
from app.data.collectors.base import BaseCollector
from app.data.normalization.events import build_external_event
from app.models.enums import EventCategory, SourceType

log = get_logger(__name__)

_NOTABLE_COMMIT_KEYWORDS = ("security", "critical", "consensus", "emergency", "hotfix",
                            "vulnerability", "51%", "halt")


class GitHubCollector(BaseCollector):
    name = "github"
    source_type = SourceType.GITHUB

    def __init__(self, sources: list[GitHubSourceCfg], bus: EventBus, *,
                 token: str = "", api_base: str = "https://api.github.com",
                 api_version: str = "2022-11-28", poll_interval_s: int = 300,
                 client: httpx.AsyncClient | None = None) -> None:
        super().__init__()
        self.sources = [s for s in sources if s.enabled]
        self.bus = bus
        self.poll_interval_s = poll_interval_s
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": api_version}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(base_url=api_base, headers=headers,
                                                   timeout=20.0)
        self._etags: dict[str, str] = {}
        self._seen_releases: set[str] = set()
        self._seen_advisories: set[str] = set()
        self._seen_commits: set[str] = set()
        self._commit_counts: dict[str, list[int]] = {}  # repo → per-poll new-commit counts
        self._archived_flagged: set[str] = set()

    async def run(self) -> None:
        self.health.healthy = True
        self.health.detail = "polling"
        while True:
            for cfg in self.sources:
                try:
                    await self.poll_repo(cfg)
                except Exception:
                    log.exception("github poll failed for %s", cfg.repo)
                    self.health.parse_errors += 1
            await asyncio.sleep(self.poll_interval_s)

    async def _get(self, path: str, params: dict | None = None) -> tuple[int, list | dict | None]:
        headers = {}
        if path in self._etags:
            headers["If-None-Match"] = self._etags[path]
        resp = await self._client.get(path, params=params, headers=headers)
        if remaining := resp.headers.get("X-RateLimit-Remaining"):
            if int(remaining) < 5:
                log.warning("github rate budget low (%s); backing off", remaining)
                await asyncio.sleep(60)
        if resp.status_code == 304:
            return 304, None
        if etag := resp.headers.get("ETag"):
            self._etags[path] = etag
        if resp.status_code >= 400:
            return resp.status_code, None
        return resp.status_code, resp.json()

    async def poll_repo(self, cfg: GitHubSourceCfg) -> list:
        events = []
        if "releases" in cfg.watch:
            events += await self._poll_releases(cfg)
        if "advisories" in cfg.watch:
            events += await self._poll_advisories(cfg)
        if "commits" in cfg.watch:
            events += await self._poll_commits(cfg)
        events += await self._poll_repo_meta(cfg)
        for event in events:
            await self.bus.publish(Topics.EXTERNAL_EVENT, event)
            self.health.mark_event()
        return events

    async def _poll_releases(self, cfg: GitHubSourceCfg) -> list:
        status, data = await self._get(f"/repos/{cfg.repo}/releases", {"per_page": 5})
        if status != 200 or not isinstance(data, list):
            return []
        out = []
        for release in data:
            key = f"{cfg.repo}#{release.get('id')}"
            if key in self._seen_releases or release.get("draft"):
                continue
            self._seen_releases.add(key)
            name = release.get("name") or release.get("tag_name", "unknown")
            out.append(build_external_event(
                text=f"{cfg.repo} release {name}",
                source=f"github:{cfg.repo}", source_type=SourceType.GITHUB,
                reliability=cfg.reliability_score,
                timestamp=_parse_ts(release.get("published_at")),
                url=release.get("html_url", ""),
                category=EventCategory.RELEASE, assets=cfg.assets,
            ))
        return out

    async def _poll_advisories(self, cfg: GitHubSourceCfg) -> list:
        status, data = await self._get(f"/repos/{cfg.repo}/security-advisories", {"per_page": 5})
        if status != 200 or not isinstance(data, list):
            return []  # advisories may be disabled — not an error
        out = []
        for adv in data:
            key = adv.get("ghsa_id", "")
            if not key or key in self._seen_advisories:
                continue
            self._seen_advisories.add(key)
            severity = adv.get("severity", "unknown")
            out.append(build_external_event(
                text=f"Security advisory {key} ({severity}) in {cfg.repo}: "
                     f"{adv.get('summary', '')[:200]}",
                source=f"github:{cfg.repo}", source_type=SourceType.GITHUB,
                reliability=min(0.9, cfg.reliability_score + 0.2),
                timestamp=_parse_ts(adv.get("published_at")),
                url=adv.get("html_url", ""),
                category=EventCategory.SECURITY_ADVISORY, assets=cfg.assets,
            ))
        return out

    async def _poll_commits(self, cfg: GitHubSourceCfg) -> list:
        status, data = await self._get(f"/repos/{cfg.repo}/commits", {"per_page": 30})
        if status != 200 or not isinstance(data, list):
            return []
        out = []
        new_count = 0
        for commit in data:
            sha = commit.get("sha", "")
            if not sha or sha in self._seen_commits:
                continue
            self._seen_commits.add(sha)
            new_count += 1
            message = ((commit.get("commit") or {}).get("message") or "").split("\n")[0]
            if any(kw in message.lower() for kw in _NOTABLE_COMMIT_KEYWORDS):
                out.append(build_external_event(
                    text=f"Notable commit in {cfg.repo}: {message[:200]}",
                    source=f"github:{cfg.repo}", source_type=SourceType.GITHUB,
                    reliability=cfg.reliability_score,
                    timestamp=_parse_ts(((commit.get("commit") or {}).get("committer") or {})
                                        .get("date")),
                    url=commit.get("html_url", ""),
                    category=EventCategory.DEVELOPMENT, assets=cfg.assets,
                ))
        counts = self._commit_counts.setdefault(cfg.repo, [])
        counts.append(new_count)
        if len(counts) > 200:
            del counts[:100]
        # activity anomaly: sudden inactivity (no commits across many polls) or burst
        if len(counts) >= 20:
            recent, history = counts[-1], counts[:-1]
            mean = sum(history) / len(history)
            if mean > 0.5 and recent > mean * 10:
                out.append(build_external_event(
                    text=f"Abnormal commit burst in {cfg.repo}: {recent} new vs mean {mean:.1f}",
                    source=f"github:{cfg.repo}", source_type=SourceType.GITHUB,
                    reliability=cfg.reliability_score,
                    timestamp=datetime.now(UTC), category=EventCategory.DEVELOPMENT,
                    assets=cfg.assets,
                ))
            if mean > 1.0 and all(c == 0 for c in counts[-20:]):
                out.append(build_external_event(
                    text=f"Development inactivity in {cfg.repo}: no commits in 20 polls",
                    source=f"github:{cfg.repo}", source_type=SourceType.GITHUB,
                    reliability=cfg.reliability_score,
                    timestamp=datetime.now(UTC), category=EventCategory.DEVELOPMENT,
                    assets=cfg.assets,
                ))
        return out

    async def _poll_repo_meta(self, cfg: GitHubSourceCfg) -> list:
        status, data = await self._get(f"/repos/{cfg.repo}")
        if status != 200 or not isinstance(data, dict):
            return []
        if data.get("archived") and cfg.repo not in self._archived_flagged:
            self._archived_flagged.add(cfg.repo)
            return [build_external_event(
                text=f"Repository {cfg.repo} was ARCHIVED",
                source=f"github:{cfg.repo}", source_type=SourceType.GITHUB,
                reliability=cfg.reliability_score, timestamp=datetime.now(UTC),
                category=EventCategory.DEVELOPMENT, assets=cfg.assets,
            )]
        return []


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
