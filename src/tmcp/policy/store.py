"""Policy loading and hot reload (PERM-01, PERM-02)."""

import asyncio
import hashlib
from pathlib import Path

import yaml
from pydantic import ValidationError

from tmcp.observability import metrics
from tmcp.observability.logging import get_logger
from tmcp.policy.engine import Permissions, resolve
from tmcp.policy.models import Policy

log = get_logger(__name__)


def parse_policy(text: str) -> Policy:
    return Policy.model_validate(yaml.safe_load(text))


class PolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        text = path.read_text(encoding="utf-8")
        self.policy = parse_policy(text)  # PERM-01: an invalid file stops the start
        self.version = hashlib.sha256(text.encode()).hexdigest()[:12]
        self._cache: dict[tuple[str, tuple[str, ...]], Permissions] = {}
        metrics.POLICY_RELOADS.inc()
        log.info("policy_loaded", version=self.version, roles=sorted(self.policy.roles), agents=len(self.policy.agents))

    @property
    def known_agents(self) -> frozenset[str]:
        return frozenset(self.policy.agents)

    def permissions(self, agent_id: str, token_roles: list[str] | None = None) -> Permissions:
        key = (agent_id, tuple(token_roles or ()))
        cached = self._cache.get(key)
        if cached is None:
            cached = resolve(self.policy, agent_id, token_roles)
            self._cache[key] = cached
        return cached

    def reload_if_changed(self) -> bool:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            metrics.POLICY_RELOAD_ERRORS.inc()
            log.error("policy_reload_failed", error=str(exc), kept_version=self.version)
            return False
        version = hashlib.sha256(text.encode()).hexdigest()[:12]
        if version == self.version:
            return False
        try:
            policy = parse_policy(text)
        except (ValidationError, yaml.YAMLError, ValueError) as exc:
            # PERM-02: keep the old policy if the new file is not valid.
            metrics.POLICY_RELOAD_ERRORS.inc()
            log.error("policy_reload_failed", error=str(exc)[:2000], kept_version=self.version, new_version=version)
            return False
        self.policy, self.version, self._cache = policy, version, {}
        metrics.POLICY_RELOADS.inc()
        log.info("policy_reloaded", version=version)
        return True

    async def watch(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            self.reload_if_changed()
