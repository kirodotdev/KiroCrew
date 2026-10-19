"""``kirocrew spawn`` and ``kirocrew artifact``: commands served by the running gateway.

Both call the dashboard's internal HTTP API on the IPv4 loopback, authenticated with
the per-session secret ``_internal_secret`` reads. ``_spawn`` lists subagents or
starts one (``_spawn_run``, which waits for the result unless told not to);
``_artifact`` lists, saves, shows, updates and deletes artifacts and lists an
artifact's versions.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig, read_local_secret
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.security import is_sensitive_path
from kiro_crew.subagent_wait_reasons import queued_wait_text
from kiro_crew.terminal_safe import safe_terminal_line


def _internal_secret(port: int) -> str:
    """Read the per-session IPC secret written by the gateway.

    The gateway writes ``~/.kiro/crew/.local_secret`` (mode 0600) after a
    successful port bind. CLI commands that hit internal API paths (e.g.
    ``/api/spawn``) send this value as ``X-Internal-Secret`` so the
    dashboard's ``token_auth_middleware`` accepts the request without a
    browser cookie. Mirrors `kiro_crew.mcp_core._internal_secret`.

    Returns an empty string if the file is missing or unreadable; the
    server then rejects the request with 403, which is the correct
    failure mode.

    Dials the IPv4 loopback LITERAL, matching the ``http://127.0.0.1`` bases its
    callers construct: a literal reaches one family, so the credential pairs with
    the address actually dialled and an ordinary single-family gateway (v4-only or
    a wildcard/container bind) still authenticates. The ambiguous ``localhost``
    would demand BOTH families and refuse such a gateway.
    """
    return read_local_secret(port, dial_host="127.0.0.1")


def _spawn_row_text(row: dict[str, object]) -> str:
    """A ``spawn list`` row's id and task, as one terminal line.

    Both are agent-authored, so neither may carry a live control sequence or a
    newline that forges another row.
    """
    row_id = safe_terminal_line(str(row["id"]))
    task = safe_terminal_line(str(row.get("task") or ""))[:60]
    return f"{row_id}  {task}"


def _spawn(args: argparse.Namespace) -> None:
    """Dispatch spawn subcommands: run, list."""
    base = f"http://127.0.0.1:{args.port}"
    action = getattr(args, "spawn_action", None)

    if action == "list":
        req = urllib.request.Request(
            f"{base}/api/spawn?queued=1",
            headers={"X-Internal-Secret": _internal_secret(args.port)},
        )
        try:
            with loopback_urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                print(f"Error: {body.get('error', e.reason)}")
            except Exception:
                print(f"Error: {e.code} {e.reason}")
            sys.exit(1)
        except (urllib.error.URLError, OSError) as e:
            # A timeout means something took the request, so the gateway is up
            # but busy -- reporting it as not running sends the operator to
            # restart a healthy gateway. A refused connection stays not running.
            # Imported in-function: a module-scope import would surface on this
            # split module's star-import facade (cli_commands).
            from kiro_crew.loopback_http import deadline_expired

            if deadline_expired(e):
                print(
                    "Error: gateway on port %d is up but did not answer in time "
                    "(busy); try again in a moment" % args.port
                )
            else:
                print("Error: gateway not running (cannot reach dashboard on port %d)" % args.port)
            sys.exit(1)
        agents = data.get("agents", [])
        queued = [q for q in data.get("queued") or [] if isinstance(q, dict) and q.get("id")]
        partial = data.get("queued_truncated") is True
        if not agents and not queued and not partial:
            print("No subagents.")
            return
        for q in queued:
            # Accepted, no run yet (or waiting to resume one): a distinct icon,
            # so this is never read as a run in progress.
            tag = "resuming" if q.get("resuming") is True else "queued, not started"
            print(f"  🕒 {_spawn_row_text(q)}  — {tag}: {queued_wait_text(q)}")
        if partial:
            print("  (the queued list is partial; more spawns may be queued)")
        for a in agents:
            if a.get("done"):
                status, note = "✅", ""
            elif a.get("awaiting_approval"):
                # A distinct icon from the executing hourglass, so `spawn list`
                # answers "is this working or waiting for me?".
                status, note = "🔐", "  — waiting for spawn approval"
            else:
                status, note = "⏳", ""
            print(f"  {status} {_spawn_row_text(a)}{note}")
        return

    if action == "run":
        _spawn_run(args, base)
        return

    print("Usage: kirocrew spawn {run|list}")


def _spawn_run(args: argparse.Namespace, base: str) -> None:
    """Spawn a subagent via the dashboard API.

    ``--agent`` and ``--crew`` travel as the request's ``agent`` and ``crew``
    fields, the ones the native ``spawn_run`` tool sends, so the gateway's
    admission resolves and refuses them for both callers alike. A blank name
    is refused here: the gateway reads an empty field as "no pin", which would
    run the default the caller did not ask for.
    """
    body: dict[str, str] = {"task": args.task}
    for flag in ("agent", "crew"):
        name = getattr(args, flag, None)
        if name is None:
            continue
        if not name.strip():
            print(f"Error: --{flag} needs a name")
            sys.exit(1)
        body[flag] = name
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}/api/spawn",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(args.port),
        },
    )
    try:
        with loopback_urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            print(f"Error: {body.get('error', e.reason)}")
        except Exception:
            print(f"Error: {e.code} {e.reason}")
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        # A timeout means something took the request, so the gateway is up but
        # busy -- reporting it as not running sends the operator to restart a
        # healthy gateway. A refused connection stays not running. The spawn
        # POST is not idempotent: the gateway may have created the subagent
        # after this client timed out, so the operator checks `spawn list`
        # rather than re-running, which would start the task twice. Imported
        # in-function: a module-scope import would surface on this split
        # module's star-import facade (cli_commands).
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(e):
            print(
                "Error: gateway on port %d is up but busy; the spawn may have "
                "started -- check `kirocrew spawn list` before re-running" % args.port
            )
        else:
            print("Error: gateway not running (cannot reach dashboard on port %d)" % args.port)
        sys.exit(1)

    agent_id = result["id"]
    # A row the gate DEFERRED (memory floor, critical posture, paused cap) is
    # accepted under this id but not running; the gateway says so with
    # ``status: "queued"`` and its own sentence, and this reader must not
    # claim a start the gateway did not make.
    queued_note = ""
    if result.get("status") == "queued":
        queued_note = str(result.get("reason_detail") or result.get("reason") or "deferred")

    if args.fire_and_forget:
        if queued_note:
            print(f"Queued subagent {agent_id}: {result['task']} (not started yet: {queued_note})")
        else:
            print(f"Spawned subagent {agent_id}: {result['task']}")
        return

    # Block: poll until done

    if queued_note:
        print(
            f"Queued subagent {agent_id} (not started yet: {queued_note}); "
            "waiting for it to start and finish...",
            file=sys.stderr,
        )
    else:
        print(f"Spawned subagent {agent_id}, waiting for result...", file=sys.stderr)
    poll_url = f"{base}/api/spawn/{agent_id}"
    secret = _internal_secret(args.port)
    told_awaiting = False
    while True:
        _time.sleep(2)
        poll_req = urllib.request.Request(poll_url, headers={"X-Internal-Secret": secret})
        try:
            with loopback_urlopen(poll_req, timeout=5) as resp:
                status = json.loads(resp.read())
        except Exception:
            print("Error: lost connection to gateway", file=sys.stderr)
            sys.exit(1)
        # Say WHY the wait is not progressing. A spawn with no parent session
        # raises its approval prompt unowned, so it appears only on the global
        # approvals surface -- not in any chat tab -- and this loop would
        # otherwise sit on "waiting for result..." indefinitely with nothing to
        # act on. Announced once, not every 2s poll.
        if status.get("awaiting_approval") and not told_awaiting:
            told_awaiting = True
            print(
                "Waiting for spawn approval: approve it in the dashboard "
                "(Approvals) to start this run.",
                file=sys.stderr,
            )
        if status.get("done"):
            if status.get("error"):
                print(f"Error: {status['error']}", file=sys.stderr)
                sys.exit(1)
            print(status.get("result", ""))
            return


def _artifact(args: argparse.Namespace) -> None:
    """List, save, view, update, or delete artifacts."""
    cfg = KiroCrewConfig.load()
    _host, port = parse_dashboard_url(cfg.dashboard.url)
    base = f"http://127.0.0.1:{port}"

    action = getattr(args, "artifact_action", None) or "list"

    headers: dict[str, str] = {"X-Internal-Secret": _internal_secret(port)}

    def _request(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers)
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{base}{path}", data=data, headers=h, method=method)
        try:
            with loopback_urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                return {"error": json.loads(exc.read()).get("error", str(exc))}
            except Exception:
                return {"error": str(exc)}
        except Exception as exc:
            return {"error": str(exc)}

    def _read_content(args: argparse.Namespace) -> str:
        if getattr(args, "content_file", None):
            p = Path(args.content_file).expanduser().resolve()
            if is_sensitive_path(str(p)):
                # Defense in depth: refuse to read credential files even
                # though the artifact API would also redact on serialize.
                print(
                    f"Error: refusing to read sensitive path: {p}",
                    file=sys.stderr,
                )
                sys.exit(1)
            return p.read_text(encoding="utf-8")
        if getattr(args, "content", None):
            return args.content
        if not sys.stdin.isatty():
            return sys.stdin.read()
        print(
            "Error: provide --content, --content-file, or pipe content via stdin", file=sys.stderr
        )
        sys.exit(1)

    def _parse_tags(s: str | None) -> list[str]:
        if not s:
            return []
        return [t.strip() for t in s.split(",") if t.strip()]

    if action == "list":
        params: list[str] = []
        for k in ("tag", "kind", "q"):
            v = getattr(args, k, None)
            if v:
                params.append(f"{k}={urllib.parse.quote(v)}")
        path = "/api/artifacts" + (f"?{'&'.join(params)}" if params else "")
        d = _request("GET", path)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        items = d.get("artifacts", [])
        if not items:
            print("No artifacts.")
            return
        for a in items:
            tags = f"  [{', '.join(a.get('tags') or [])}]" if a.get("tags") else ""
            print(
                f"{a.get('slug', '?'):<40s}  v{a.get('version', '?'):<3} "
                f"{a.get('kind', '?'):<10}{tags}  {a.get('name', '?')}"
            )
        return

    if action == "show":
        slug = args.slug
        version = getattr(args, "version", None)
        path = f"/api/artifacts/{slug}"
        if version:
            path = f"/api/artifacts/{slug}/versions/{int(version)}"
        d = _request("GET", path)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        if getattr(args, "meta", False):
            d.pop("content", None)
            print(json.dumps(d, indent=2))
        else:
            print(d.get("content") or "")
        return

    if action == "save":
        body: dict = {
            "name": args.name,
            "content": _read_content(args),
            "tags": _parse_tags(getattr(args, "tags", None)),
        }
        # An explicit --slug is forwarded whenever it is not None, INCLUDING the
        # empty string. "" is a slug the caller named, not a request to derive
        # one, and the store refuses it; truthy filtering would swallow it and
        # silently take the derive-and-suffix branch instead.
        slug_arg = getattr(args, "slug", None)
        if slug_arg is not None:
            body["slug"] = slug_arg
        for k in ("kind", "description"):
            v = getattr(args, k, None)
            if v:
                body[k] = v
        d = _request("POST", "/api/artifacts", body)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        slug = d.get("slug", "?")
        print(f"Saved: slug={slug} version={d.get('version', 1)}")
        # Present only when the slug was derived from --name, because that is the
        # branch where a taken slug resolves by suffixing. That reads as success
        # (exit 0, "version=1"), which is how a re-save of corrected content ends
        # up published at a slug nobody looks at while the original keeps serving
        # the old text. Name the slug that was taken and the verb that versions in
        # place. An explicit --slug cannot land here: the store refuses it rather
        # than renaming — 409 when the slug is taken, 400 when it is malformed
        # (the empty string included) — so both surface on the error path above.
        taken = d.get("slug_collided_with")
        if taken:
            print(
                f"Warning: slug '{taken}' is already taken, so this created a NEW "
                f"artifact at '{slug}' rather than a new version of '{taken}'. "
                f"To version the existing artifact in place, use: "
                f"kirocrew artifact update {taken}",
                file=sys.stderr,
            )
        # Relayed from the gateway (same pattern as slug_collided_with): the
        # handler computes the theme-contrast verdict at the convergence
        # point, so this CLI path -- the one the motivating incident traveled
        # -- hears it without duplicating the detector. Advisory only.
        if d.get("theme_contrast_warning"):
            print(
                "Warning: content hardcodes colors with no theme variables — it "
                "will clash in dark/light/custom dashboard themes. Prefer "
                "var(--text,#111) / var(--bg,#fff) style fallbacks (see the "
                "widgets skill's theme table).",
                file=sys.stderr,
            )
        return

    if action == "update":
        slug = args.slug
        body = {}
        # Only read stdin when explicit content args are absent. In non-
        # interactive environments (CI, cron, piped /dev/null) sys.stdin.isatty()
        # returns False even when the user only intends a metadata update —
        # if we read stdin unconditionally, an empty pipe would send
        # content="" and wipe the artifact's content. Require an explicit
        # content arg or non-empty stdin to overwrite content.
        if getattr(args, "content", None) or getattr(args, "content_file", None):
            body["content"] = _read_content(args)
        elif not sys.stdin.isatty():
            stdin_data = sys.stdin.read()
            if stdin_data:
                body["content"] = stdin_data
        if getattr(args, "name", None):
            body["name"] = args.name
        if getattr(args, "description", None) is not None:
            body["description"] = args.description
        if getattr(args, "tags", None) is not None:
            body["tags"] = _parse_tags(args.tags)
        if not body:
            print("Error: provide content/--name/--description/--tags", file=sys.stderr)
            sys.exit(1)
        d = _request("PATCH", f"/api/artifacts/{slug}", body)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"Updated: slug={d.get('slug', slug)} version={d.get('version', '?')}")
        # Relayed from the gateway (see the save action above) -- the PATCH
        # path is exactly how the motivating incident's script pushed its
        # hardcoded-palette page. Advisory only; the update succeeded.
        if d.get("theme_contrast_warning"):
            print(
                "Warning: content hardcodes colors with no theme variables — it "
                "will clash in dark/light/custom dashboard themes. Prefer "
                "var(--text,#111) / var(--bg,#fff) style fallbacks (see the "
                "widgets skill's theme table).",
                file=sys.stderr,
            )
        return

    if action == "delete":
        slug = args.slug
        d = _request("DELETE", f"/api/artifacts/{slug}")
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"Deleted: {slug}")
        return

    if action == "versions":
        slug = args.slug
        d = _request("GET", f"/api/artifacts/{slug}/versions")
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        versions = d.get("versions", [])
        if not versions:
            print(f"No versions for {slug}.")
        else:
            print(", ".join(f"v{v}" for v in versions))
        return

    print(
        "Usage: kirocrew artifact {list|show|save|update|delete|versions}",
        file=sys.stderr,
    )
    sys.exit(2)
