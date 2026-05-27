"""
Jarvis — WorldMonitor REST integration.

WorldMonitor (https://www.worldmonitor.app) exposes an AI-agent-first API at
https://api.worldmonitor.app with an OpenAPI spec at /openapi.yaml. Pro-tier
endpoints require an API key sent as `X-WorldMonitor-Key: wm_live_...`.

We only wire the two officially published agent skills here:
  - fetch-country-brief    -> /api/intelligence/v1/get-country-intel-brief
  - fetch-resilience-score -> /api/resilience/v1/get-resilience-score

Used by server.py for the [ACTION:WM_BRIEF] and [ACTION:WM_SCORE] voice actions.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx

API_BASE = "https://api.worldmonitor.app"
HEADER_NAME = "X-WorldMonitor-Key"

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


def _key() -> str:
    cfg = _load_config()
    key = (cfg.get("worldmonitor_api_key") or "").strip()
    if not key or key.startswith("YOUR_") or key == "wm_live_PASTE_HERE":
        raise RuntimeError(
            "WorldMonitor-API-Key fehlt. Setze 'worldmonitor_api_key' in config.json. "
            "Issue under https://www.worldmonitor.app/pro."
        )
    return key


def is_configured() -> bool:
    try:
        _key()
        return True
    except Exception:
        return False


def _client(timeout: float = 20.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={HEADER_NAME: _key(), "Accept": "application/json"},
        timeout=timeout,
    )


def _normalize_country_code(value: str) -> str:
    """Uppercase + strip; reject blanks. WorldMonitor enforces uppercase server-side."""
    v = (value or "").strip().upper()
    if len(v) != 2 or not v.isalpha():
        raise ValueError(f"country code muss ISO 3166-1 alpha-2 sein (zwei Buchstaben), erhielt: {value!r}")
    return v


async def get_country_brief(country_code: str, framework: Optional[str] = None) -> dict:
    """
    Retrieve the AI-generated strategic intelligence brief for a country.
    `framework` optionally appends analytical framing (e.g. "focus on energy security").
    """
    cc = _normalize_country_code(country_code)
    params: dict[str, str] = {"country_code": cc}
    if framework:
        params["framework"] = framework[:2000]
    async with _client() as c:
        r = await c.get("/api/intelligence/v1/get-country-intel-brief", params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"country-brief {r.status_code}: {r.text[:200]}")
        return r.json()


async def get_resilience_score(country_code: str) -> dict:
    """Composite resilience score 0-100 with domain / pillar breakdown."""
    cc = _normalize_country_code(country_code)
    async with _client() as c:
        r = await c.get(
            "/api/resilience/v1/get-resilience-score",
            params={"countryCode": cc},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"resilience-score {r.status_code}: {r.text[:200]}")
        return r.json()


async def health() -> dict:
    """Public health endpoint — usable without API key for connectivity checks."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://www.worldmonitor.app/api/health")
        r.raise_for_status()
        return r.json()
