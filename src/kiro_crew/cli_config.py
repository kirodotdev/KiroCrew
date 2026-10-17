"""CLI config subcommand — get, set, edit configuration values."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import beacon
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    ConfigReadError,
    ConfigWriteRefused,
    _subtract_overlay,
    config_local_path,
    config_path,
    read_config_text,
    update_config_locked,
    workspace_dir_from_entry,
)
from kiro_crew.config.sections import _migrate_workspaces
from kiro_crew.config.superseded_defaults import (
    acked_superseded,
    adopt_coerced_keys,
    adopted_superseded,
    adoption_summary,
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

if TYPE_CHECKING:
    from kiro_crew.config.schema import ConfigEntry

_MISSING = object()

#: How the OS sandbox refuses a write to a sealed config file: Seatbelt answers
#: ``EPERM``, and a Linux read-only file bind answers ``EROFS`` for an in-place write
#: and ``EBUSY`` for the rename ``atomic_write`` publishes with.
_SEALED_CONFIG_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS, errno.EBUSY})


def _names_sealed_config(exc: OSError) -> bool:
    """Whether *exc* was raised against ``config.json`` or ``config.local.json``.

    The publishing ``os.replace(tmp, path)`` reports the temp as ``filename`` and the
    destination as ``filename2``; an in-place ``open(path, "w")`` reports only
    ``filename``. Either way the sealed file is one of the two. A failure that names
    neither -- ``config edit``'s ``execvp`` of an editor, a temp the data home itself
    refused -- is the caller's to report as what it is.
    """
    sealed = {os.path.realpath(p) for p in (config_path(), config_local_path())}
    for name in (exc.filename, exc.filename2):
        if isinstance(name, (str, bytes, os.PathLike)):
            if os.path.realpath(os.fsdecode(name)) in sealed:
                return True
    return False


def _sandboxed_config_write_hint(exc: OSError) -> str | None:
    """The operator-facing reason a config write failed inside the agent sandbox.

    ``config.json`` and ``config.local.json`` are read-only to every sandboxed process
    (``sandbox._CREW_READONLY_LEAVES``) because they carry the switches that loosen
    confinement. Without this the refusal surfaces as a bare errno, which reads like a
    broken install. Decided from the failure itself -- a denial errno against one of
    the two sealed files -- and not from ``KIROCREW_SANDBOX_ACTIVE``: ``cli.main()``
    pops that marker before dispatch so an inherited value can never buy a sandbox
    bypass, which means it is never set by the time this runs. ``None`` for any other
    failure, so the caller's own error path still reports a genuinely read-only or
    full data home, or an editor the sandbox would not exec.
    """
    if exc.errno not in _SEALED_CONFIG_ERRNOS:
        return None
    if not _names_sealed_config(exc):
        return None
    return (
        "❌ The config file was not written. Inside the agent sandbox config.json and "
        "config.local.json are read-only, so an agent cannot change the settings that "
        "confine it: change the setting in the dashboard (Settings), or run this command "
        "from your own terminal. Outside the sandbox, check the file's permissions."
    )


def _refuse_missing_workspace_dirs(data: dict, current: dict) -> dict:
    """Refuse a new or changed ``workspaces`` entry whose ``dir`` is not an existing directory.

    Same stance as ``workspace update``. The base dir is exempt: it is made on first use.
    """
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        return data
    on_disk = current.get("workspaces")
    old = _migrate_workspaces(on_disk) if isinstance(on_disk, dict) else {}
    base = workspace_dir_from_entry(None)
    for name, entry in _migrate_workspaces(workspaces).items():
        if name in old and old[name].dir == entry.dir:
            continue  # an unchanged binding is not this write's to judge
        try:
            path = workspace_dir_from_entry(entry)
            usable = isinstance(entry.dir, str) and (path == base or path.is_dir())
        except (RuntimeError, OSError, TypeError, ValueError):  # unknown ``~user``, bad type
            usable = False
        if not usable:
            raise ConfigWriteRefused(
                f"workspace {name!r}: dir {entry.dir!r} is not an existing directory"
            )
    return data


def _config_cmd(args: argparse.Namespace) -> None:
    """Get or set config values."""
    try:
        _run_config_cmd(args)
    except OSError as exc:
        hint = _sandboxed_config_write_hint(exc)
        if hint is None:
            raise
        print(hint, file=sys.stderr)
        sys.exit(1)


def _run_config_cmd(args: argparse.Namespace) -> None:
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
            try:
                update_config_locked(
                    config_path(),
                    mutate=lambda cur: _refuse_missing_workspace_dirs(data, cur),
                    on_corrupt="reset",
                )
            except ConfigWriteRefused as e:
                # Third writer behind the publish floor, same report as the keyed
                # paths: nothing was written. The message names an env-var key
                # (never its value) or a workspace and its dir. Anchored on the file,
                # since there is no single key to name.
                print(f"❌ {fp}: {e}", file=sys.stderr)
                sys.exit(1)
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
            # A list-typed key takes a list or is refused: the generic parser
            # answers an unparsable word with the STRING itself, and a string
            # stored under a list key is replaced by the default at load.
            parsed: object
            try:
                if _is_list_key(key):
                    parsed = _parse_list_value(key, value)
                else:
                    parsed = _parse_value(value)
            except ValueError as list_error:
                print(f"❌ {key}: {list_error}", file=sys.stderr)
                sys.exit(1)
            # A declared enum is checked on EVERY write, stored or not, and what is
            # written is the enum's own spelling. The type check below runs only on
            # a first write, because a stored value's type stands in for the
            # declaration; an enum has no such stand-in, and the load path's answer
            # to a value outside it is to degrade the setting with a WARNING nobody
            # reads. `stt.provider off` was accepted this way while `off` did not
            # exist, and the loader turned it into the one provider the user was
            # trying to escape (kirodotdev/KiroCrew#13179).
            try:
                parsed = _declared_enum_value(key, parsed)
            except ValueError as enum_error:
                print(f"❌ {key}: {enum_error}", file=sys.stderr)
                sys.exit(1)
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

                try:
                    update_config_locked(
                        p, mutate=_mutate_local_overlay, stamp_meta=False, on_corrupt="reset"
                    )
                except ConfigWriteRefused as e:
                    # The publish floor refused the document before writing it; the
                    # message names the offending env-var key and never its value.
                    print(f"❌ {key}: {e}", file=sys.stderr)
                    sys.exit(1)

                sel().log_api_access(
                    caller="cli",
                    operation="config_set_local",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)} (saved to config.local.json)")
            else:
                # Validate the key exists before taking the lock. A declared
                # leaf counts as existing even when it is not stored yet, but
                # then its value is type-checked here: this is its first write,
                # so no stored value stands in for the declaration.
                cfg = KiroCrewConfig.load()
                d = cfg.to_dict()
                if not _dict_set(d, key, parsed):
                    declared = _declared_entry(key)
                    if declared is None:
                        print(f"❌ Unknown key: {key}", file=sys.stderr)
                        sys.exit(1)
                    type_error = _declared_type_error(declared, parsed)
                    if type_error:
                        print(f"❌ {key}: {type_error}", file=sys.stderr)
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
                            raw_local = json.loads(read_config_text(lp))
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
                except ConfigWriteRefused as e:
                    # Same shape as the corrupt-file refusal: nothing was written, and
                    # the message names the offending env-var key, never its value.
                    print(f"❌ {key}: {e}", file=sys.stderr)
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
    wanted = list(getattr(args, "keys", None) or [])
    listing = not (getattr(args, "adopt", False) or getattr(args, "keep", False) or wanted)
    if listing:
        # BEFORE config.json is opened: a ledger can outlive or outlast the file it
        # describes, and what an earlier load removed is still the answer to the
        # question this command was asked -- a missing or corrupt config must not
        # hide it. Listing only; an adopted key is not an --adopt/--keep target.
        _print_adopted()
    path = config_path()
    try:
        raw = json.loads(read_config_text(path))
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
        rewritten: dict[str, str] = {}
        coerced_keys = [c.dotted_key for c, _ in coerced]

        def _mutate(existing: dict) -> dict:
            # Re-detect inside the lock: the document read above is a snapshot, and
            # removing a key whose value changed since would discard a live choice.
            fresh = [
                e.dotted_key
                for e in superseded_default_drift(existing, acked={})
                if e.dotted_key in keys
            ]
            removed.extend(drop_drifted_keys(existing, fresh))
            # A coerced key is not dropped but REWRITTEN as what the loader resolves
            # it to (or dropped only when that is the default), so adopting never
            # moves the effective setting: an unknown speech provider that runs as
            # `off` stays `off`, rather than becoming the default `local` that the
            # user may have been trying to escape.
            live_coerced = [
                (c, v) for c, v in coerced_value_drift(existing) if c.dotted_key in coerced_keys
            ]
            for c, v in live_coerced:
                value = c.adopted_value(v)
                if value is not None:
                    rewritten[c.dotted_key] = value
            removed.extend(adopt_coerced_keys(existing, live_coerced))
            return existing

        try:
            update_config_locked(config_path(), mutate=_mutate)
        except ConfigReadError as e:
            print(f"❌ Cannot edit a corrupt config.json: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            # A read-only or full data home, or a refused link: report it and stop,
            # rather than letting the CLI die on a traceback.
            print(
                _sandboxed_config_write_hint(e) or f"❌ Could not write {config_path()}: {e}",
                file=sys.stderr,
            )
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
            if key in rewritten:
                print(f"✅ {key} set to {rewritten[key]!r} — what the stored value already ran as")
            elif key in overridden:
                print(f"✅ {key} removed from config.json — config.local.json still overrides it")
            else:
                print(f"✅ {key} removed — the current default now applies")
        if not removed:
            print("Nothing removed — the stored values changed since they were listed.")
        # The schema's restart=True mark is the one statement of which fields a
        # running gateway cannot adopt, so the restart hint names exactly those keys
        # -- and only where the effective value moved: an overlay-shadowed key runs
        # the same value after the edit as before it.
        # Imported here: the schema builds the full registry at import time, which
        # the listing and --keep paths never need.
        from kiro_crew.config.schema import requires_restart

        restart_bound = [key for key in removed if key not in overridden and requires_restart(key)]
        if restart_bound:
            print(
                "\nRestart the gateway for a running instance to pick up: "
                + ", ".join(restart_bound)
            )
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
        notes = {e.dotted_key: e.note for e in drifted if e.note}
        for key in recorded:
            print(f"✅ {key} recorded as intentional — no longer reported")
            if key in notes:
                # Said again at the moment of affirming: the note is a fact the
                # choice to keep depends on, and this is the last line they read.
                print(f"   Note: {notes[key]}.")
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


def _print_adopted() -> None:
    """List what the load path already auto-adopted, with the undo for each.

    Listing only: an adopted key holds no stored value any more, so it is neither
    drift nor something ``--adopt``/``--keep`` can act on, and it is deliberately not
    folded into the ``known`` set those flags validate against. It is shown because
    the only other announcement was one WARNING line in a gateway log, and "what did
    the upgrade change in my file" is exactly the question a bare listing asks.
    """
    for dotted, removed in sorted(adopted_superseded().items()):
        print(f"ℹ️  {adoption_summary(dotted, removed)}\n")


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
        raw = json.loads(read_config_text(p))
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


def _declared_entry(key: str) -> ConfigEntry | None:
    """The ``SCHEMA_REGISTRY`` entry for *key*, or None when nothing declares it.

    ``_dict_set`` navigates the CURRENT document, so a declared sub-key of a
    dict-typed field - ``dashboard.terminal.shell``, ``completion.enabled`` - is
    unreachable from ``config set`` until something else writes it first: the
    field's ``default_factory`` does not carry it and nothing merges the schema's
    defaults into a stored dict. This restores the key the schema promises.

    Membership in ``SCHEMA_REGISTRY`` is the strict reading of "declared", and
    the reason the check is not a walk of ``JSON_SCHEMA``: such a field sets
    ``additionalProperties: True``, so a walk would also accept a typo under it
    and write the misspelling to disk. A wildcard entry (``agents.*``) does not
    match a concrete path here, which leaves those keys exactly as they were.

    The entry itself is returned, not a bool, because the caller must also check
    the value against the declared type: this is the key's FIRST write, so there
    is no stored value whose type could stand in for the declaration.

    Imported lazily to mirror ``config.validation``: the registry is built at
    import time, and the CLI's other verbs should not pay for it.
    """
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    for entry in SCHEMA_REGISTRY:
        if entry.path == key:
            return entry
    return None


#: Declared JSON Schema type -> the Python types ``_parse_value`` may produce for
#: it. Absent from this table means "no opinion": a type the CLI cannot usefully
#: check (or a future one) must not become a refusal.
_DECLARED_VALUE_TYPES: dict[str, tuple[type, ...]] = {
    "array": (list,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "object": (dict,),
    "string": (str,),
}


def _declared_enum_value(key: str, value: object) -> object:
    """*value* as the enum for *key* spells it, or *value* itself when *key* has no enum.

    Raises ``ValueError`` with the message to print when *value* is outside the
    enum. Unlike :func:`_declared_type_error` this runs on every write, stored or
    not, because the load path degrades an out-of-enum value rather than rejecting
    it -- so the write is the last point at which the mistake is still attributable
    to the command that made it. A key that declares no enum, or is not declared at
    all, keeps the behaviour it had.

    Spelling is the one leniency, and the CANONICAL spelling is what comes back:
    the loader normalizes case on some enum keys (``agent.log_level`` is
    upper-cased, ``agent.yolo_duration`` lower-cased and stripped) but matches
    others exactly, so writing the user's spelling would admit ``Local`` here and
    have the loader degrade it there -- the write-then-degrade gap this check
    exists to close. Writing the enum's own spelling closes it for every key at
    once. A value the loader would degrade is exactly what this check refuses.
    """
    entry = _declared_entry(key)
    if entry is None or not entry.enum_values:
        return value
    if value in entry.enum_values:
        return value
    if isinstance(value, str):
        if key == "stt.model":
            resolved = _stt_model_canonical(value)
            if resolved is not None:
                return resolved
        folded = value.strip().casefold()
        for candidate in entry.enum_values:
            if isinstance(candidate, str) and candidate.casefold() == folded:
                return candidate
    allowed = ", ".join(str(v) for v in entry.enum_values)
    raise ValueError(f"{value!r} is not one of the selectable values: {allowed}")


def _stt_model_canonical(value: str) -> str | None:
    """The catalog row a stored ``stt.model`` spelling selects, or ``None``.

    ``stt.model`` is the one enum whose list holds CANONICAL rows while its
    loader also accepts aliases onto them. The write admits the alias and stores
    the row it names, the same way the dashboard's STT PUT does through
    ``stt_models.canonical_name`` -- a bare membership test would refuse
    ``stt.model turbo`` that the loader resolves to ``large-v3-turbo`` on every
    load. A second such key would earn a table; one does not.
    """
    from kiro_crew.stt import models as stt_models

    return stt_models.canonical_name(value)


def _declared_type_error(entry: ConfigEntry, value: object) -> str | None:
    """Why *value* does not fit *entry*'s declared type, or None when it fits.

    ``_parse_value`` cannot fail: an unparsable word becomes the string itself,
    so ``config set <integer key> nope`` would otherwise store ``"nope"`` and
    surface as a TypeError deep in whatever compares it (a terminal open reads
    ``max_sessions`` and does ``len(registry) >= max_sessions``). Refusing at the
    write is the only place the mistake is still attributable to the command.

    Checked ONLY on the first write of a declared-but-unstored key, which is the
    path :func:`_declared_entry` opens. A key that is already in the document
    keeps its historical behaviour, type check included: widening this to every
    ``config set`` is a larger, separate change.
    """
    expected = _DECLARED_VALUE_TYPES.get(entry.type)
    if expected is None:
        return None
    if value is None and entry.nullable:
        return None
    # bool is a subclass of int, so an integer leaf would silently accept `true`.
    if isinstance(value, bool) and bool not in expected:
        return f"expected {entry.type}, got boolean"
    if not isinstance(value, expected):
        return f"expected {entry.type}, got {type(value).__name__}"
    return None


def _is_list_key(key: str) -> bool:
    """True when *key* is declared ``array`` in the registry.

    The declaration is the authority (a wildcard path such as
    ``telegram.accounts.*.allowed_user_ids`` matches segment by segment). A key the
    registry does not know returns False: the base write path refuses an unknown
    key outright before this is consulted, so there is no reachable undeclared key
    for the loader-reset harm to apply to.
    """
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    parts = key.split(".")
    for entry in SCHEMA_REGISTRY:
        e_parts = entry.path.split(".")
        if len(e_parts) == len(parts) and all(
            e == "*" or e == p for e, p in zip(e_parts, parts, strict=True)
        ):
            return entry.type == "array"
    return False


def _parse_list_value(key: str, raw: str) -> list:
    """Parse *raw* as the value of the list-typed *key*, or raise ``ValueError``.

    Only a JSON array is accepted. The case that matters is the array whose quotes
    a shell removed (Windows PowerShell 5.1 delivers ``'["a","b"]'`` as ``[a,b]``):
    it is not JSON, so the scalar parser stored it as a string and the loader then
    dropped the whole field back to its default, un-trusting whatever the list
    held. No other spelling is guessed at: a list field's item type (``list[int]``
    for a Telegram user id, ``list[str]`` for a Slack one) is not known here, and a
    wrongly typed list is dropped the same way.
    """
    text = raw.strip()
    try:
        loaded = json.loads(text)
    except ValueError:
        raise ValueError(f"expected a JSON array, got {raw!r}\n" + _list_hint(key)) from None
    if isinstance(loaded, list):
        return loaded
    raise ValueError(
        f"expected a JSON array, got a JSON {type(loaded).__name__}\n" + _list_hint(key)
    )


def _list_hint(key: str) -> str:
    """The retry lines for a refused list value: the forms that survive PowerShell.

    Both the example items (``["a","b"]``) and the key are rendered so that nothing
    the caller typed can break out of the retry line: the hint is printed for a
    human or an agent to paste into a shell, so a shell metacharacter in either
    would let the pasted command run something else. The items are a STATIC
    example; the key is shown verbatim only when it is a plain config dot-path
    (the only shape a real key has) and is replaced by a ``<key>`` placeholder
    otherwise, so a wildcard segment carrying ``;`` or a quote never reaches the
    shell. The task is to demonstrate the quoting that survives each shell, which
    a safe key and fixed items do.
    """
    as_json = '["a","b"]'
    escaped = as_json.replace('"', '\\"')
    safe_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.*-")
    safe_key = key if key and set(key) <= safe_chars else "<key>"
    prefix = f"kirocrew config set {safe_key}"
    return "\n".join(
        [
            "   Nothing was written. A shell may have stripped the quotes of a JSON array",
            "   (Windows PowerShell 5.1 does), leaving [a,b], which is not JSON. Retry with:",
            f"     PowerShell 7.3+, sh:    {prefix} '{as_json}'",
            f"     Windows PowerShell 5.1: {prefix} '{escaped}'",
        ]
    )


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
