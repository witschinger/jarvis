#!/usr/bin/env python3
"""
Register an Asana webhook against the running Jarvis server.

Usage:
  python3 scripts/register-asana-webhook.py --target https://abc.trycloudflare.com/asana/webhook
  python3 scripts/register-asana-webhook.py --target ... --resource <gid>
  python3 scripts/register-asana-webhook.py --list
  python3 scripts/register-asana-webhook.py --delete <webhook_gid>

Without --resource, the webhook is registered for the entire workspace
(asana_workspace_gid in config.json, or first workspace on the account).
"""

import argparse
import asyncio
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)

import asana_tools  # noqa: E402


DEFAULT_FILTERS = [
    {"resource_type": "task", "action": "added"},
    {"resource_type": "task", "action": "changed"},
    {"resource_type": "task", "action": "deleted"},
    {"resource_type": "task", "action": "removed"},
    {"resource_type": "task", "action": "undeleted"},
    {"resource_type": "story", "action": "added"},
]


async def cmd_register(target: str, resource: str | None, no_filters: bool) -> int:
    if not asana_tools.is_configured():
        print("Asana PAT fehlt in config.json (Feld 'asana_pat').", file=sys.stderr)
        return 2

    if resource:
        res = resource
        scope_label = "(custom resource)"
    else:
        # Default: watch the authenticated user's "My Tasks" list. This is what
        # the workspace-level "all task events for me" intent actually maps to;
        # workspace gids reject task filters as "larger-scoped".
        res = await asana_tools.get_my_user_task_list_gid()
        scope_label = "(My Tasks list — alle eigenen Aufgaben)"

    filters = None if no_filters else DEFAULT_FILTERS
    print(f"Registering webhook:")
    print(f"  resource = {res} {scope_label}")
    print(f"  target   = {target}")
    print(f"  filters  = {filters if filters else '(none)'}")
    print(f"Asana wird sofort einen Handshake-POST schicken — der Jarvis-Server muss laufen.")
    try:
        result = await asana_tools.create_webhook(res, target, filters=filters)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(f"OK — webhook gid {result.get('gid')} active={result.get('active')}")
    return 0


async def cmd_list() -> int:
    if not asana_tools.is_configured():
        print("Asana PAT fehlt in config.json.", file=sys.stderr)
        return 2
    hooks = await asana_tools.list_webhooks()
    if not hooks:
        print("Keine Webhooks fuer diesen Workspace.")
        return 0
    for h in hooks:
        res = (h.get("resource") or {}).get("name", "?")
        print(f"  {h.get('gid')}  active={h.get('active')}  resource={res}  target={h.get('target')}")
    return 0


async def cmd_list_projects() -> int:
    if not asana_tools.is_configured():
        print("Asana PAT fehlt in config.json.", file=sys.stderr)
        return 2
    projects = await asana_tools.list_projects(limit=100)
    if not projects:
        print("Keine aktiven Projekte gefunden.")
        return 0
    print(f"{len(projects)} aktive Projekte:")
    for p in projects:
        print(f"  {p['gid']}  {p['name']}")
    return 0


async def cmd_delete(gid: str) -> int:
    if not asana_tools.is_configured():
        print("Asana PAT fehlt in config.json.", file=sys.stderr)
        return 2
    await asana_tools.delete_webhook(gid)
    print(f"Deleted webhook {gid}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", help="Public webhook URL Asana should POST to (e.g. https://X.trycloudflare.com/asana/webhook).")
    p.add_argument("--resource", help="Asana resource gid to watch. Defaults to workspace.")
    p.add_argument("--no-filters", action="store_true", help="Skip the default task/story filters (only valid for project-scoped webhooks; workspace-scoped webhooks require filters).")
    p.add_argument("--list", action="store_true", help="List existing webhooks.")
    p.add_argument("--list-projects", action="store_true", help="List active Asana projects with gids — useful for picking asana_default_project_gid.")
    p.add_argument("--delete", metavar="GID", help="Delete the webhook with this gid.")
    args = p.parse_args()

    if args.list_projects:
        return asyncio.run(cmd_list_projects())
    if args.list:
        return asyncio.run(cmd_list())
    if args.delete:
        return asyncio.run(cmd_delete(args.delete))
    if args.target:
        return asyncio.run(cmd_register(args.target, args.resource, args.no_filters))
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
