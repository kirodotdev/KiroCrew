"""CLI config subcommand — get, set, edit configuration values."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from kiro_crew import beacon
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    ConfigReadError,
    _subtract_overlay,
    config_local_path,
    config_path,
    update_config_locked,
)
from kiro_crew.config.superseded_defaults import (
    acked_superseded,
    coerced_value_drift,
    coercion_summary,
    drift_summary,
    drop_acks,
    drop_drifted_keys,
    record_acks,
    superseded_default_drift,
)
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
            if not isinstance(data, dict):
                # A JSON array or scalar parses fine but is not a config. Refusing
                # here keeps the file untouched; writing it through would leave a
                # config.json that every reader rejects.
                print(
                    f"❌ Not a config object: {fp} holds a JSON "
                    f"{type(data).__name__}, expected an object",
                    file=sys.stderr,
                )
                sys.exit(1)
            update_config_locked(config_path(), mutate=lambda _: data, on_corrupt="reset")
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
            # Fourth write path to telemetry.beacon_enabled, after the dashboard
            # PATCH and `telemetry enable`. Gated here too, and BEFORE the
            # local/base split so it covers both: `--local` writes the overlay,
            # which takes precedence over the base file, so leaving it ungated
            # would make the generic setter the one way to store `true` on a
            # pinned host — the same false-promise-on-a-privacy-control failure
            # the 403 exists to prevent. Only the enable direction is refused
            # (tightest-wins), matching the other two chokepoints.
            if key == "telemetry.beacon_enabled" and parsed is True:
                # Audited for the same reason as the other enforcement calls, with
                # its own tool name so the trail says which control refused.
                if beacon.is_governance_pinned_off(audit_tool="config_set_cli"):
                    print(
                        "❌ The anonymous beacon is pinned OFF by your "
                        "administrator's security policy (capabilities.telemetry).",
                        file=sys.stderr,
                    )
                    print(
                        "   Not writing config — the setting would have no effect.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            # Same shape for the tailnet origin derivation, and placed here for the
            # same reason: BEFORE the local/base split, so `--local` (whose overlay
            # takes precedence over the base file) cannot become the one way to
            # store `true` on a pinned host. Only the enable direction is refused
            # (tightest-wins), matching the PATCH 403 and the startup gate.
            if (
                key in ("dashboard.tailscale.enabled", "dashboard.tailscale.trust_identity")
                and parsed is True
            ):
                from kiro_crew.dashboard import tailnet

                if tailnet.is_governance_pinned_off(audit_tool="config_set_cli_tailnet"):
                    print(
                        "❌ Tailnet dashboard access is pinned OFF by your "
                        "administrator's security policy "
                        "(capabilities.tailnet_origin).",
                        file=sys.stderr,
                    )
                    print(
                        "   Not writing config — the setting would have no effect.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            if use_local:
                top_key = key.split(".")[0]
                _known_sections = {f.name for f in dataclasses.fields(KiroCrewConfig)}
                if top_key not in _known_sections:
                    print(
                        f"⚠️  Warning: '{top_key}' is not a recognized config section",
                        file=sys.stderr,
                    )
                p = config_local_path()

                # NOTE: unlike the automatic/background config writers (which now
                # fail closed via read_config_for_update), this interactive path
                # deliberately overwrites a corrupt overlay — the user typed an
                # explicit `config set --local` and sees the result on stdout.
                # Pinned by test_config_overlay.py::TestCliConfigSetLocal.
                #
                # on_corrupt="reset" handles the corrupt case inside the same
                # lock hold: the mutate callback receives {} and writes the
                # single key from scratch. No second critical section needed.
                def _mutate_local_overlay(_existing: dict) -> dict:
                    _dict_set_create(_existing, key, parsed)
                    return _existing

                update_config_locked(
                    p, mutate=_mutate_local_overlay, stamp_meta=False, on_corrupt="reset"
                )

                sel().log_api_access(
                    caller="cli",
                    operation="config_set_local",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)} (saved to config.local.json)")
            else:
                # Validate the key exists before taking the lock.
                cfg = KiroCrewConfig.load()
                d = cfg.to_dict()
                if not _dict_set(d, key, parsed):
                    print(f"❌ Unknown key: {key}", file=sys.stderr)
                    sys.exit(1)

                def _mutate_base(existing: dict) -> dict:
                    # Apply the set on the freshly-locked raw data.
                    # Key was already validated above; use _dict_set_create so
                    # sections that were never written (still at defaults) get
                    # their intermediate keys created.
                    _dict_set_create(existing, key, parsed)
                    lp = config_local_path()
                    if lp.is_file():
                        try:
                            raw_local = json.loads(lp.read_text(encoding="utf-8"))
                            if isinstance(raw_local, dict):
                                return _subtract_overlay(existing, raw_local)
                        except (json.JSONDecodeError, OSError):
                            pass
                    return existing

                try:
                    update_config_locked(config_path(), mutate=_mutate_base)
                except ConfigReadError as e:
                    print(
                        f"❌ Cannot set key in a corrupt config.json: {e}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
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
            print(f"👻 Created default config: {p}")
        sel().log_api_access(
            caller="cli",
            operation="config_edit",
            outcome="allowed",
            source="cli",
            resources=str(p),
        )
        editor = os.environ.get("EDITOR", "vi")
        os.execvp(editor, [editor, str(p)])
    elif action == "defaults":
        _defaults_cmd(args)
    else:
        print("Usage: kirocrew config {get,set,edit,defaults}", file=sys.stderr)
        sys.exit(1)


def _defaults_cmd(args: argparse.Namespace) -> None:
    """Review, adopt, or affirm stored values that no longer match the shipped ones.

    Two kinds, and the difference decides what an operator may do:

    - a SUPERSEDED DEFAULT still wins, so it may be a deliberate choice. It can be
      adopted away or affirmed.
    - a COERCED value cannot win -- the loader replaces it at parse time -- so there
      is nothing to affirm and ``--keep`` refuses it by name rather than promising a
      setting that never takes effect.

    Three modes over one detection pass, so the list an operator reads and the set a
    flag acts on cannot disagree:

    - no flag: list both kinds, marking anything already affirmed.
    - ``--adopt``: remove the keys, so the loader resolves the current default.
      Rewriting is safe here where it is not on the load path because the operator
      asked by name, and only a key that IS drifted or coerced is ever removed.
      Detection runs again inside the write lock, so a value changed since it was
      listed is left alone.
    - ``--keep``: record the stored values as intentional, silencing the load-path
      notice for exactly those values. Changing a value later reports it again.

    A bare ``--adopt``/``--keep`` skips keys already affirmed, so it cannot silently
    undo an earlier decision; naming a key explicitly still acts on it. A key that
    is neither drifted nor coerced is refused rather than silently ignored, because
    a typo would otherwise read as success.
    """
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("✅ No config.json yet — the current defaults already apply.")
        return
    except OSError as e:
        print(f"❌ Could not read {path}: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        # ValueError covers both shapes of unreadable content without restating
        # them: json.JSONDecodeError and UnicodeDecodeError are both subclasses.
        print(f"❌ Could not read {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(raw, dict):
        print(f"❌ {path} is not a JSON object", file=sys.stderr)
        sys.exit(1)

    # acked={} so the operator sees the whole truth; an ack answers the unsolicited
    # load-path line, not the question they just typed.
    drifted = superseded_default_drift(raw, acked={})
    coerced = coerced_value_drift(raw)
    acked = acked_superseded()
    wanted = list(getattr(args, "keys", None) or [])
    if wanted:
        known = {e.dotted_key for e in drifted} | {c.dotted_key for c, _ in coerced}
        unknown = [k for k in wanted if k not in known]
        if unknown:
            print(
                "❌ Not holding a superseded default or a coerced value: " + ", ".join(unknown),
                file=sys.stderr,
            )
            print(
                "   Run 'kirocrew config defaults' with no arguments to see the list.",
                file=sys.stderr,
            )
            sys.exit(1)
        drifted = [e for e in drifted if e.dotted_key in wanted]
        coerced = [(c, v) for c, v in coerced if c.dotted_key in wanted]

    if not drifted and not coerced:
        print("✅ No stored value holds a superseded default.")
        return

    keeping = getattr(args, "keep", False)
    if keeping and coerced:
        # There is nothing to affirm: the loader replaces the value whatever the
        # operator thinks of it, so recording it as intentional would promise a
        # setting that never takes effect.
        for centry, stored in coerced:
            print(
                f"⚠️  {centry.dotted_key} ({stored!r}) cannot be kept — "
                f"the loader replaces it. Use --adopt to drop it."
            )
        coerced = []
        if not drifted:
            return

    if (getattr(args, "adopt", False) or keeping) and not wanted:
        # A bare --adopt/--keep means "all of them", and an affirmed value is not
        # one of them: the operator already answered for that key, so sweeping it
        # up here would silently undo their decision. Naming a key explicitly
        # still acts on it -- that is the operator saying so again.
        drifted = [e for e in drifted if e.dotted_key not in acked]
        if not drifted and not coerced:
            print("✅ Nothing left to act on — every remaining value is acknowledged.")
            print("   Name a key explicitly to change one of those.")
            return

    keys = [e.dotted_key for e in drifted]

    if getattr(args, "adopt", False):
        removed: list[str] = []
        coerced_keys = [c.dotted_key for c, _ in coerced]

        def _mutate(existing: dict) -> dict:
            # Re-detect inside the lock: the document read above is a snapshot, and
            # removing a key whose value changed since would discard a live choice.
            fresh = [
                e.dotted_key
                for e in superseded_default_drift(existing, acked={})
                if e.dotted_key in keys
            ]
            fresh += [
                c.dotted_key
                for c, _ in coerced_value_drift(existing)
                if c.dotted_key in coerced_keys
            ]
            removed.extend(drop_drifted_keys(existing, fresh))
            return existing

        try:
            update_config_locked(config_path(), mutate=_mutate)
        except ConfigReadError as e:
            print(f"❌ Cannot edit a corrupt config.json: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            # A read-only or full data home, or a refused link: report it and stop,
            # rather than letting the CLI die on a traceback.
            print(f"❌ Could not write {config_path()}: {e}", file=sys.stderr)
            sys.exit(1)
        # An adopted key no longer stores the acked value, so its ack is dead
        # bookkeeping; dropping it keeps a later deliberate choice reportable.
        try:
            if removed:
                drop_acks(removed)
        except OSError as e:
            print(f"⚠️  Adopted, but could not update acknowledgments: {e}", file=sys.stderr)
        sel().log_api_access(
            caller="cli",
            operation="config_adopt_defaults",
            outcome="allowed",
            source="cli",
            resources=",".join(removed) or "none",
        )
        # An overlay value outranks the base file, so for a key it also carries the
        # EFFECTIVE value does not change -- saying the default now applies would be
        # false. Report what actually happened instead.
        overridden = _overlay_keys(removed)
        for key in removed:
            if key in overridden:
                print(f"✅ {key} removed from config.json — config.local.json still overrides it")
            else:
                print(f"✅ {key} removed — the current default now applies")
        if not removed:
            print("Nothing removed — the stored values changed since they were listed.")
        else:
            print("\nRestart the gateway for a running instance to pick this up.")
        return

    if keeping:
        try:
            recorded = record_acks(keys)
        except ConfigReadError as e:
            print(f"❌ Cannot read a corrupt config.json: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"❌ Could not record acknowledgments: {e}", file=sys.stderr)
            sys.exit(1)
        if not recorded:
            print("Nothing recorded — the stored values changed since they were listed.")
            return
        sel().log_api_access(
            caller="cli",
            operation="config_keep_defaults",
            outcome="allowed",
            source="cli",
            resources=",".join(recorded),
        )
        for key in recorded:
            print(f"✅ {key} recorded as intentional — no longer reported")
        print("\nChanging one of these values later reports it again.")
        return

    for entry in drifted:
        mark = "✅ acknowledged as intentional" if entry.dotted_key in acked else "ℹ️ "
        print(f"{mark} {drift_summary(entry)}\n")
    for centry, stored in coerced:
        print(f"ℹ️  {coercion_summary(centry, stored)}\n")
    if coerced or any(e.dotted_key not in acked for e in drifted):
        print("Take the current defaults:  kirocrew config defaults --adopt")
        print("Affirm your values:         kirocrew config defaults --keep")
        print("Either accepts specific keys, e.g. --keep session.autocompact_pct")


def _overlay_keys(dotted_keys: list[str]) -> set[str]:
    """Return the subset of *dotted_keys* that ``config.local.json`` also carries.

    Removing such a key from the base file does not change the value the loader
    resolves -- the overlay still wins -- so the adopt report must not claim the
    current default now applies. Reads only, and treats an unreadable overlay as
    carrying nothing: a wrong claim is worse than a missing note either way, and
    every other surface already tolerates a broken overlay.
    """
    p = config_local_path()
    if not p.is_file():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers malformed JSON and invalid UTF-8 alike; either way the
        # overlay is treated as carrying nothing.
        return set()
    if not isinstance(raw, dict):
        return set()
    found = set()
    for dotted in dotted_keys:
        if _dict_get(raw, dotted) is not _MISSING:
            found.add(dotted)
    return found


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
    except ValueError:
        pass
    return raw
