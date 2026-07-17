"""CLI config subcommand — get, set, edit configuration values."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import _subtract_overlay, config_local_path, config_path
from kiro_crew.hooks import safe_read_file
from kiro_crew.sel import sel

_MISSING = object()


def _config_cmd(args: argparse.Namespace) -> None:
    """Get or set config values."""
    action = getattr(args, "config_action", None)
    if action == "get":

        cfg = KiroCrewConfig.load()
        d = cfg.to_dict()
        key = getattr(args, "key", None)
        sel().log_api_access(
            caller="cli",
            operation="config_get",
            outcome="allowed",
            source="cli",
            resources=key or "*",
        )
        if not key:
            print(json.dumps(d, indent=2))
            return
        val = _dict_get(d, key)
        if val is _MISSING:
            print(f"❌ Unknown key: {key}", file=sys.stderr)
            sys.exit(1)
        if isinstance(val, (dict, list)):
            print(json.dumps(val, indent=2))
        else:
            print(val)
    elif action == "set":

        file_path = getattr(args, "file", None)
        if file_path:
            fp = Path(file_path).expanduser().resolve()

            try:
                data = json.loads(safe_read_file(str(fp)))
            except PermissionError as e:
                print(f"❌ {e}", file=sys.stderr)
                sys.exit(1)
            except (json.JSONDecodeError, OSError) as e:
                print(f"❌ Invalid JSON: {e}", file=sys.stderr)
                sys.exit(1)
            p = config_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            sel().log_api_access(
                caller="cli",
                operation="config_set_file",
                outcome="allowed",
                source="cli",
                resources=str(fp),
            )
            print(f"✅ Config loaded from {file_path}")
        else:
            key = args.key
            value = args.value
            use_local = getattr(args, "local", False)
            if not key or value is None:
                print("Usage: kirocrew config set <key> <value>", file=sys.stderr)
                print("       kirocrew config set --local <key> <value>", file=sys.stderr)
                print("       kirocrew config set --file <path.json>", file=sys.stderr)
                sys.exit(1)
            parsed = _parse_value(value)
            if use_local:
                top_key = key.split(".")[0]
                _known_sections = {f.name for f in dataclasses.fields(KiroCrewConfig)}
                if top_key not in _known_sections:
                    print(
                        f"⚠️  Warning: '{top_key}' is not a recognized config section",
                        file=sys.stderr,
                    )
                p = config_local_path()
                try:
                    d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                except (json.JSONDecodeError, OSError):
                    d = {}
                if not isinstance(d, dict):
                    d = {}
                _dict_set_create(d, key, parsed)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
                sel().log_api_access(
                    caller="cli",
                    operation="config_set_local",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)} (saved to config.local.json)")
            else:
                cfg = KiroCrewConfig.load()
                d = cfg.to_dict()
                if not _dict_set(d, key, parsed):
                    print(f"❌ Unknown key: {key}", file=sys.stderr)
                    sys.exit(1)
                lp = config_local_path()
                if lp.is_file():
                    try:
                        raw_local = json.loads(lp.read_text(encoding="utf-8"))
                        if isinstance(raw_local, dict):
                            d = _subtract_overlay(d, raw_local)
                    except (json.JSONDecodeError, OSError):
                        pass
                p = config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
                sel().log_api_access(
                    caller="cli",
                    operation="config_set",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)}")
    elif action == "edit":

        p = config_path()
        if not p.exists():
            cfg = KiroCrewConfig()
            cfg.save()
            print(f"🐾 Created default config: {p}")
        sel().log_api_access(
            caller="cli",
            operation="config_edit",
            outcome="allowed",
            source="cli",
            resources=str(p),
        )
        editor = os.environ.get("EDITOR", "vi")
        os.execvp(editor, [editor, str(p)])
    else:
        print("Usage: kirocrew config {get,set,edit}", file=sys.stderr)
        sys.exit(1)


def _dict_get(d: dict, key: str) -> object:
    """Get a value from a nested dict using dot-separated key."""
    parts = key.split(".")
    cur: object = d
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _dict_set(d: dict, key: str, value: object) -> bool:
    """Set a value in a nested dict using dot-separated key. Returns False if parent missing."""
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    if not isinstance(cur, dict):
        return False
    if parts[-1] not in cur:
        return False
    cur[parts[-1]] = value
    return True


def _dict_set_create(d: dict, key: str, value: object) -> None:
    """Set a value in a nested dict, creating intermediate dicts as needed."""
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _parse_value(raw: str) -> object:
    """Parse a CLI value string into the appropriate Python type."""
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    return raw
