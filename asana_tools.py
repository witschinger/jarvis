"""
Jarvis — Asana REST integration.
Personal-Access-Token auth, async via httpx. Used by server.py for voice
actions (LIST/CREATE/DONE) and the morning briefing, and by the webhook
helper scripts in scripts/.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

ASANA_BASE = "https://app.asana.com/api/1.0"

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_workspace_cache: Optional[str] = None


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


def _pat() -> str:
    cfg = _load_config()
    pat = (cfg.get("asana_pat") or "").strip()
    if not pat or pat.startswith("YOUR_") or pat == "1/PASTE_YOUR_PAT":
        raise RuntimeError(
            "Asana PAT fehlt. Setze 'asana_pat' in config.json "
            "(https://app.asana.com/0/my-apps -> Personal Access Token)."
        )
    return pat


def is_configured() -> bool:
    """True iff a non-placeholder PAT is present in config.json."""
    try:
        _pat()
        return True
    except Exception:
        return False


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=ASANA_BASE,
        headers={"Authorization": f"Bearer {_pat()}", "Accept": "application/json"},
        timeout=timeout,
    )


async def whoami() -> dict:
    """Connection sanity check — returns the authenticated user."""
    async with _client() as c:
        r = await c.get("/users/me", params={"opt_fields": "name,email,workspaces.name,workspaces.gid"})
        r.raise_for_status()
        return r.json().get("data", {})


async def resolve_workspace_gid() -> str:
    """Return configured workspace_gid or fall back to the first workspace on the account."""
    global _workspace_cache
    cfg = _load_config()
    configured = (cfg.get("asana_workspace_gid") or "").strip()
    if configured:
        return configured
    if _workspace_cache:
        return _workspace_cache
    me = await whoami()
    workspaces = me.get("workspaces") or []
    if not workspaces:
        raise RuntimeError("Kein Asana-Workspace fuer diesen Account gefunden.")
    _workspace_cache = workspaces[0]["gid"]
    return _workspace_cache


async def list_my_tasks(limit: int = 20, project_gid: Optional[str] = None) -> list[dict]:
    """
    Incomplete tasks assigned to the authenticated user.

    If `project_gid` is given (or `asana_default_project_gid` is set in config —
    the "Jarvis Inbox" project), tasks are restricted to that project (Asana's
    /tasks endpoint forbids combining assignee=me with project, so we hit
    /projects/{gid}/tasks and filter assignee client-side). Otherwise returns
    all incomplete tasks across the workspace.
    """
    cfg = _load_config()
    project = project_gid or (cfg.get("asana_default_project_gid") or "").strip() or None
    page_limit = min(max(limit, 1), 100)
    opt_fields = "name,due_on,completed,assignee.gid,projects.name"

    async with _client() as c:
        if project:
            me = await whoami()
            my_gid = me.get("gid")
            r = await c.get(
                f"/projects/{project}/tasks",
                params={
                    "completed_since": "now",
                    "limit": page_limit,
                    "opt_fields": opt_fields,
                },
            )
            r.raise_for_status()
            tasks = r.json().get("data", [])
            return [t for t in tasks if (t.get("assignee") or {}).get("gid") == my_gid]
        else:
            r = await c.get(
                "/tasks",
                params={
                    "assignee": "me",
                    "workspace": await resolve_workspace_gid(),
                    "completed_since": "now",
                    "limit": page_limit,
                    "opt_fields": opt_fields,
                },
            )
            r.raise_for_status()
            return r.json().get("data", [])


async def create_task(
    name: str,
    notes: Optional[str] = None,
    project_gid: Optional[str] = None,
    assignee: Optional[str] = "me",
    due_on: Optional[str] = None,
) -> dict:
    """
    Create a task. Defaults to assigning to the authenticated user so that the
    new task lands in My Tasks (and triggers user_task_list webhooks).
    `due_on` is the calendar-date due date in ISO format (YYYY-MM-DD).
    """
    cfg = _load_config()
    project = project_gid or (cfg.get("asana_default_project_gid") or "").strip() or None

    payload: dict[str, Any] = {"data": {"name": name}}
    if notes:
        payload["data"]["notes"] = notes
    if assignee:
        payload["data"]["assignee"] = assignee
    if due_on:
        payload["data"]["due_on"] = due_on
    if project:
        payload["data"]["projects"] = [project]
    else:
        payload["data"]["workspace"] = await resolve_workspace_gid()

    async with _client() as c:
        r = await c.post("/tasks", json=payload)
        r.raise_for_status()
        return r.json().get("data", {})


async def complete_task(task_gid: str) -> dict:
    async with _client() as c:
        r = await c.put(f"/tasks/{task_gid}", json={"data": {"completed": True}})
        r.raise_for_status()
        return r.json().get("data", {})


async def find_task_by_name(query: str) -> Optional[dict]:
    """Fuzzy task lookup via workspace typeahead. Returns first hit or None."""
    workspace = await resolve_workspace_gid()
    async with _client() as c:
        r = await c.get(
            f"/workspaces/{workspace}/typeahead",
            params={"resource_type": "task", "query": query, "count": 1, "opt_fields": "name,gid,completed"},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return data[0] if data else None


async def get_my_user_task_list_gid() -> str:
    """
    Return the gid of the authenticated user's "My Tasks" list for the configured workspace.
    Watching this resource is the correct way to receive task-level webhooks at user scope —
    workspace-scoped webhooks reject task filters.
    """
    workspace = await resolve_workspace_gid()
    async with _client() as c:
        r = await c.get("/users/me/user_task_list", params={"workspace": workspace, "opt_fields": "gid,name"})
        r.raise_for_status()
        data = r.json().get("data") or {}
        gid = data.get("gid")
        if not gid:
            raise RuntimeError("user_task_list lookup returned no gid.")
        return gid


async def get_task(task_gid: str, opt_fields: str = "name,assignee.name,due_on,completed,projects.name") -> dict:
    """Fetch a single task by gid (used by webhook handler for announcements)."""
    async with _client() as c:
        r = await c.get(f"/tasks/{task_gid}", params={"opt_fields": opt_fields})
        r.raise_for_status()
        return r.json().get("data", {})


async def list_workspaces() -> list[dict]:
    async with _client() as c:
        r = await c.get("/workspaces", params={"opt_fields": "name,gid"})
        r.raise_for_status()
        return r.json().get("data", [])


async def create_project(
    name: str,
    workspace_gid: Optional[str] = None,
    privacy_setting: str = "public_to_workspace",
    notes: Optional[str] = None,
) -> dict:
    """Create a new Asana project in the configured (or given) workspace."""
    workspace = workspace_gid or await resolve_workspace_gid()
    payload: dict[str, Any] = {
        "data": {"workspace": workspace, "name": name, "privacy_setting": privacy_setting}
    }
    if notes:
        payload["data"]["notes"] = notes
    async with _client() as c:
        r = await c.post("/projects", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"create_project failed {r.status_code}: {r.text}")
        return r.json().get("data", {})


async def list_projects(workspace_gid: Optional[str] = None, limit: int = 50) -> list[dict]:
    workspace = workspace_gid or await resolve_workspace_gid()
    async with _client() as c:
        r = await c.get(
            "/projects",
            params={"workspace": workspace, "limit": min(limit, 100), "opt_fields": "name,gid,archived"},
        )
        r.raise_for_status()
        return [p for p in r.json().get("data", []) if not p.get("archived")]


# --- Webhook management -------------------------------------------------------

async def create_webhook(resource_gid: str, target_url: str, filters: Optional[list[dict]] = None) -> dict:
    """
    Register an Asana webhook. Asana will immediately call `target_url` once with
    an `X-Hook-Secret` header that the receiver must echo back to confirm.

    `filters` is optional (e.g. [{"resource_type": "task", "action": "added"}]).
    Without filters, Asana sends all events for the resource.
    """
    payload: dict[str, Any] = {"data": {"resource": resource_gid, "target": target_url}}
    if filters:
        payload["data"]["filters"] = filters
    async with _client(timeout=30) as c:
        r = await c.post("/webhooks", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"Webhook registration failed {r.status_code}: {r.text}")
        return r.json().get("data", {})


async def list_webhooks(workspace_gid: Optional[str] = None) -> list[dict]:
    workspace = workspace_gid or await resolve_workspace_gid()
    async with _client() as c:
        r = await c.get("/webhooks", params={"workspace": workspace, "opt_fields": "resource.name,target,active"})
        r.raise_for_status()
        return r.json().get("data", [])


async def delete_webhook(webhook_gid: str) -> None:
    async with _client() as c:
        r = await c.delete(f"/webhooks/{webhook_gid}")
        r.raise_for_status()
