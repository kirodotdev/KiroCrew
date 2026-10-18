"""``kirocrew update``: bring this install up to date from its git checkout, a wheel
release or its managed venv.

Holds the update command with the memory snapshot it takes first, the check against a
running gateway's own update state, the agent-config refresh after an update, and the
wheel and managed-venv paths. ``kirocrew update approve`` is in ``cli_server_requests``.

Split out of ``kiro_crew.cli_server``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.cli_server.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New update work goes here.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from kiro_crew import dep_sync, platform_compat
from kiro_crew.beacon import distribution
from kiro_crew.cli_server_peer import (
    _CLI_LOOPBACK,
    _GatewayPeerRefused,
    _owner_verified_secret_urlopen,
)
from kiro_crew.config.loader import read_local_secret
from kiro_crew.frontend import build_frontend_sync
from kiro_crew.git_divergence import (
    UNREADABLE_UNPARSEABLE,
    DivergenceUnreadable,
    count_divergence_sync,
)
from kiro_crew.kiro_cli import PATH_ONLY_INSTALL_NOTE, is_bundled_kiro_cli, pin_kiro_cli
from kiro_crew.platform.update_capability import (
    EXTERNALLY_MANAGED_MESSAGES,
    MANAGED_BY_GIT,
    derive_capability,
)
from kiro_crew.port_resolution import resolve_client_port
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def _snapshot_memory_or_exit() -> None:
    """Copy every memory store, or refuse to let the caller rewrite this install.

    Called immediately before each updater, never once at the top of ``_update``: the
    interval guard is bypassed here, so a no-op or refused update must not spend a
    retention slot on a copy nothing was going to change. ``memory.backup_keep`` is
    passed for the same reason -- the sweep's own default would prune a longer
    configured history. A copy that did not land exits 1, because rewriting the install
    around the one store nothing else can rebuild is the loss this prevents.
    """
    from kiro_crew import memory_backup
    from kiro_crew.platform.wheel_apply import snapshot_memory_before_update, snapshot_refusal

    copied, failure = snapshot_memory_before_update()
    if failure:
        print(f"  ❌ {snapshot_refusal(failure)}")
        sys.exit(1)
    # Named as the DEFAULT store's directory: a bare path beside a count covering every
    # store would point at the wrong place.
    newest = memory_backup.newest_backup()
    where = f"; default store copies in {newest.parent}" if newest is not None else ""
    print(f"  💾 Memory snapshot: {copied} store(s) copied{where}\n")


def _update(force: bool = False) -> None:
    """Update Kiro Crew — dispatches based on install layout.

    Three install layouts, three update paths:

    * **git checkout** — fetch + reset --hard + rebuild (existing path).
      The reset only runs for a FAST-FORWARDABLE checkout (behind its
      upstream, not ahead). A checkout that has DIVERGED (committed local
      work both ahead of and behind ``origin/<branch>``) is refused: the
      hard reset would discard the local commits, and the tracked-change
      prompt only covers uncommitted edits. ``force=True`` (the ``--force``
      CLI flag) is the explicit opt-in that lets the reset discard them.
      An ahead-only checkout has nothing to pull and is reported as up to
      date without resetting.
    * **wheel / cli.sh** — fetch the release feed, compare versions, and
      re-run the installer if newer. This is the path that was missing and
      caused the ``KIROCREW_PROJECT_DIR not set`` error for cli.sh installs.
    * **externally managed** (desktop app, Docker) — print guidance on how
      to update via the correct surface instead of failing with an opaque error.
    """
    from kiro_crew.platform.update_layout import InstallLayout

    print("👻 Updating Kiro Crew…\n")

    # A policy-defined provider OWNS the update on this host. Checked before any
    # layout dispatch so a manual `kirocrew update` cannot run the built-in
    # git/CDN mechanism the administrator excluded.
    from kiro_crew.platform.update_provider import apply_policy_update, resolve_provider

    # A provider that is not configured to apply -- check-only, or on Windows, where
    # every provider command refuses -- is not about to rewrite anything.
    _provider = resolve_provider()
    if _provider is not None and _provider.can_apply():
        _snapshot_memory_or_exit()
    applied = asyncio.run(apply_policy_update())
    if applied is not None:
        if applied:
            print("\n✅ Update applied by the policy-defined update command.")
            print("\n  Restart the gateway to use the new version:")
            print("    kirocrew restart")
        else:
            print("\n❌ The policy-defined update command failed — see the log above.")
            print("  Not falling back to the built-in updater: this host's policy")
            print("  selects its own update mechanism.")
            sys.exit(1)
        return

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    # Dispatch on the shared derivation, not on the git probe alone. The probe
    # answers "is this a working tree", which is NOT the same question as "who
    # owns updating this install": a container or a desktop bundle pointed at a
    # checkout would otherwise take the git path here and reset a tree its own
    # updater owns. `derive_capability` puts the externally managed stamp first
    # for exactly that reason, and this is the surface that has to honour it.
    capability = derive_capability(install_root=proj)

    if capability.managed_by != MANAGED_BY_GIT:
        if capability.defers:
            reason = capability.unavailable_reason or ""
            print(f"  ℹ️  This install ({distribution()}) is managed externally.")
            print(f"  {EXTERNALLY_MANAGED_MESSAGES.get(reason, '')}")
            return

        # Wheel / cli.sh install path.
        layout = InstallLayout(
            kind=distribution() or "wheel",
            proj=proj,
            is_git=False,
            is_externally_managed=False,
            guidance="",
        )
        _update_wheel(layout)
        return

    print(f"  📂 {proj}")

    # Detect current branch
    try:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "git rev-parse timed out after %ss during update", exc.timeout
        )
        print("❌ Could not determine current branch (git rev-parse timed out)")
        sys.exit(1)
    if branch_result.returncode != 0:
        print("❌ Could not determine current branch")
        sys.exit(1)
    branch = branch_result.stdout.strip() or "mainline"
    if branch == "HEAD":
        branch = "mainline"

    # Source pin, checked before the fetch so a blocked update never touches the
    # tree. A human at a terminal is not the authorization: the fleet decides
    # which remote this host may take code from.
    from kiro_crew.platform.update_governance import resolve_remote_url, update_blocked_reason

    _blocked = update_blocked_reason(resolve_remote_url(proj, remote="origin"))
    if _blocked:
        print(f"  🛡️  Update blocked by security policy: {_blocked}")
        sys.exit(1)

    # Fetch + reset --hard: no merge conflicts, untracked files preserved
    print("  ⬇️  git fetch…")
    try:
        result = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "git fetch timed out after %ss during update", exc.timeout
        )
        print(f"  ❌ git fetch timed out after {exc.timeout}s")
        sys.exit(1)
    if result.returncode != 0:
        print(f"  ❌ git fetch failed:\n{result.stderr.strip()}")
        sys.exit(1)

    # Pin the fetched upstream to one commit. Every judgment below -- "already
    # up to date", the divergence counts, the interpreter floor -- and the reset
    # itself name this OID rather than origin/<branch>, so they all describe
    # the same revision by construction: a fetch run concurrently from another
    # terminal can move the name, never the pin. Spelled as the full
    # remote-tracking ref: the short form is resolved with tags ahead of
    # remotes, so a tag named `origin/<branch>` -- which a fetch auto-follows
    # from the remote -- would otherwise be what every judgment and the reset
    # took as the upstream.
    try:
        pin_result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"],
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "git rev-parse timed out after %ss during update", exc.timeout
        )
        print(f"  ❌ Could not resolve origin/{branch} (git rev-parse timed out)")
        sys.exit(1)
    target = pin_result.stdout.strip()
    if pin_result.returncode != 0 or not target:
        print(f"  ❌ Could not resolve origin/{branch}:\n{pin_result.stderr.strip()}")
        sys.exit(1)

    # Check if there are new commits
    try:
        diff_result = subprocess.run(
            ["git", "diff", "HEAD", target, "--quiet"],
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        up_to_date = diff_result.returncode == 0
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "git diff timed out after %ss during update", exc.timeout
        )
        # Same branch a non-zero exit takes: assume new commits exist and let
        # the divergence guard below re-classify before anything destructive.
        print("  ⚠️  git diff timed out — continuing to the divergence check")
        up_to_date = False
    if up_to_date:
        print("\n✅ Already up to date!")
        return

    # Divergence guard. The hard reset below discards local COMMITTED work,
    # and the tracked-change prompt after this only sees uncommitted edits —
    # a checkout carrying its own commits passes that prompt silently.
    # Mirror the dashboard check's verdict: only a fast-forwardable checkout
    # (behind and not ahead) proceeds to the reset; ahead-only has nothing to
    # pull and returns without resetting; true divergence refuses unless the
    # operator explicitly opted in with --force. Counted against the pinned
    # commit — exactly what the reset targets.
    #
    # This runs TWICE — once here, once immediately before the reset — because
    # the prompt below makes the gap to the destructive step unbounded. Both
    # calls go through one classifier so the two sites differ only in what they
    # PRINT, never in which states they recognise: a state handled in one and
    # forgotten in the other is how a guard grows a hole.
    def _divergence_verdict() -> tuple[str, int, int]:
        """Classify HEAD against the pinned upstream commit for the reset decision.

        Returns ``(verdict, ahead, behind)`` where verdict is one of:

        * ``"unreadable"`` — the comparison could not be read; the caller must
          refuse, since a guard that cannot count must not wave a destructive
          reset through.
        * ``"up_to_date"`` — nothing to pull (``behind == 0``), so
          origin/<branch> is an ancestor of HEAD and the reset could only
          REMOVE commits. Never resettable, ``--force`` included: that flag
          exists to let a real update discard diverged work, not to delete
          commits when there is nothing to update to.
        * ``"diverged"`` — ahead AND behind; resettable only under ``--force``.
        * ``"fast_forward"`` — behind and not ahead; nothing of its own to lose.
        """
        counts = count_divergence_sync(proj, target)
        if isinstance(counts, DivergenceUnreadable):
            if counts.reason == UNREADABLE_UNPARSEABLE:
                print(f"  ❌ Could not parse the commit counts against origin/{branch}:")
                print(f"     {counts.detail!r}")
            else:
                print(f"  ❌ Could not compare HEAD against origin/{branch}:")
                print(f"     {counts.detail}")
            return "unreadable", -1, -1
        ahead, behind = counts.ahead, counts.behind
        if behind == 0:
            return "up_to_date", ahead, behind
        if ahead > 0:
            return "diverged", ahead, behind
        return "fast_forward", ahead, behind

    def _report_up_to_date(ahead: int) -> None:
        suffix = f" ({ahead} local commit(s) ahead of origin/{branch})" if ahead else ""
        print(f"\n✅ Already up to date!{suffix}")

    verdict, ahead, behind = _divergence_verdict()
    if verdict == "unreadable":
        sys.exit(1)
    if verdict == "up_to_date":
        _report_up_to_date(ahead)
        return
    if verdict == "diverged":
        if not force:
            print(f"  ⚠️  This checkout has diverged from origin/{branch}:")
            print(f"      {ahead} local commit(s) not on origin/{branch}, {behind} behind.")
            print("  A hard reset would discard the local commits. Reconcile instead:")
            print(f"      git rebase origin/{branch}    (or: git merge origin/{branch})")
            print("  Or discard the local commits explicitly:")
            print("      kirocrew update --force")
            sys.exit(1)
        print(f"  ⚠️  --force: discarding {ahead} local commit(s) not on origin/{branch}.")

    # Interpreter floor of the pinned revision, read from the commit itself. The
    # reinstall below would refuse it anyway, but only after the reset has moved
    # the tree to code this venv cannot import -- leaving a running gateway
    # serving old code out of memory while every lazy import reads the new
    # files, and every later `kirocrew update` repeating the reset and the
    # refusal. Judged here: before the operator is asked to discard anything,
    # and outside the re-classification that must stay adjacent to the reset.
    try:
        floor_breach = dep_sync.incoming_python_floor_breach(
            Path(proj), target, Path(sys.executable)
        )
    except dep_sync.IncomingFloorUnreadable as exc:
        # Unreadable is not absent: a git failure here must refuse, or it is
        # the one way the revision the gate exists to keep out still lands.
        print(f"  ❌ Could not read the incoming revision's interpreter requirement: {exc}")
        sys.exit(1)
    if floor_breach:
        print(f"  ❌ Refusing to update: {floor_breach}")
        sys.exit(1)

    # Warn about local tracked-file changes before discarding
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "git status timed out after %ss during update", exc.timeout
        )
        # Fail closed: this check exists to warn before the hard reset
        # discards local tracked changes, so an unreadable answer must
        # refuse the reset — same stance as the unreadable-divergence guard.
        print("  ❌ Could not check for local changes (git status timed out)")
        sys.exit(1)
    tracked_changes = [
        line for line in status.stdout.strip().splitlines() if not line.startswith("??")
    ]
    if tracked_changes:
        print("  ⚠️  Local tracked-file changes will be discarded:")
        for line in tracked_changes[:10]:
            print(f"      {line}")
        resp = input("  Continue? [y/N] ").strip().lower()
        if resp != "y":
            print("  Aborted.")
            sys.exit(0)

    # Re-classify immediately before the reset. The verdict above is a
    # snapshot, and the prompt makes the gap to the reset unbounded:
    # committing the listed changes in another terminal is the natural way to
    # rescue them, and rebasing them onto the upstream afterwards is the
    # natural next step — the first leaves the snapshot stale, the second
    # turns the checkout ahead-only, and both end in the reset deleting the
    # commits the operator just made to save that work. Only HEAD can move
    # here: the reset targets the pinned commit, which no concurrent fetch can
    # rewrite, so this needs no second network round trip.
    verdict, ahead, behind = _divergence_verdict()
    if verdict == "unreadable":
        sys.exit(1)
    if verdict == "up_to_date":
        # Not an error: the operator's own commits made the update unnecessary.
        # Unresettable even under --force, exactly as in the first pass.
        _report_up_to_date(ahead)
        return
    if verdict == "diverged" and not force:
        print(f"  ⚠️  Refusing to reset: {ahead} local commit(s) appeared on HEAD while")
        print("      this update was waiting, which a hard reset would discard.")
        print(f"      Reconcile with: git rebase origin/{branch}")
        sys.exit(1)

    _snapshot_memory_or_exit()
    print(f"  🔄 git reset --hard origin/{branch} ({target[:12]})…")
    try:
        result = subprocess.run(
            ["git", "reset", "--hard", target],
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "git reset timed out after %ss during update", exc.timeout
        )
        print(f"  ❌ git reset timed out after {exc.timeout}s")
        sys.exit(1)
    if result.returncode != 0:
        print(f"  ❌ git reset failed:\n{result.stderr.strip()}")
        sys.exit(1)

    # Update the optional kiro-cli backend if present. Pinned to an absolute
    # path from the known install directories, with the inherited PATH excluded:
    # a bare argv0 is re-resolved inside exec against a PATH that can lead with
    # an agent-writable directory, and a `which` probe's answer is not what exec
    # would run. No pin -> skip the step; a PATH-only install is reported so the
    # operator knows why it was skipped and which override puts it back.
    kiro_cli_bin, unpinned_kiro_cli = pin_kiro_cli()
    if kiro_cli_bin is None and unpinned_kiro_cli:
        print(f"  ⚠️  kiro-cli update skipped: {PATH_ONLY_INSTALL_NOTE}")
    if kiro_cli_bin is not None and is_bundled_kiro_cli(kiro_cli_bin, os.environ):
        # The desktop app's bundled copy lives inside the signed bundle and is
        # replaced by the app update, never in place.
        print("  ℹ️  kiro-cli is the desktop app's bundled copy; it updates with the app")
        kiro_cli_bin = None
    if kiro_cli_bin is not None:
        print("  🔄 kiro-cli update")
        try:
            subprocess.run(
                [kiro_cli_bin, "update"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            logging.getLogger("kiro_crew.cli_server").warning(
                "kiro-cli update timed out after %ss; skipping (best-effort)", exc.timeout
            )
            print("  ⚠️  kiro-cli update timed out — run manually: kiro-cli update")

    # Ensure a supported Node.js for frontend builds
    from kiro_crew.cli import _ensure_node  # circular import: cli -> cli_server -> cli

    print("  🔄 Checking Node.js…")
    _ensure_node(proj)

    # Build the dashboard frontend assets (npm), then reinstall the package.
    build_frontend_sync(Path(proj))

    # Install the pulled revision into this CLI's own venv. `kirocrew update` is
    # itself run FROM the console script pip would have to rewrite, so on Windows
    # the reinstall cannot succeed and dep_sync substitutes a dependency-only sync
    # (it reports, rather than silently tolerating, a revision that repointed the
    # script — that is the one case still needing a terminal without kirocrew
    # running).
    print("  🔨 Installing the pulled revision…")

    def _emit(message: str, error: bool) -> None:
        print(f"  {'❌' if error else '•'} {message}")

    rc = dep_sync.sync_or_reinstall(Path(proj), Path(sys.executable), _emit)
    if rc != 0:
        sys.exit(1)

    print("\n✅ Kiro Crew updated!")

    _refresh_agent_config(proj)

    # Reconcile the running gateway's update badge. The reset above moved the
    # tree, but a long-lived gateway keeps serving the pre-update verdict from
    # its cache until the next 12-hourly poll, a manual check, or a restart --
    # so the About panel shows "Update available" for a checkout that is now
    # current. Poke it to drop the cache and re-check. Best-effort: the update
    # has already succeeded, so a gateway that is not running or a call that
    # fails must not change the outcome or the exit code.
    _revalidate_gateway_update_check()


def _revalidate_gateway_update_check() -> None:
    """Best-effort: tell the running gateway to re-check for updates now.

    Called at the end of a successful git-checkout update so the About panel's
    "Update available" badge reconciles immediately instead of waiting for the
    next scheduled poll. Reuses the CLI's own loopback transport and the
    per-generation local secret, the same pair `kirocrew token` and
    `kirocrew logout` use to reach the gateway.

    No gateway (no secret to reach one) returns silently — the next boot
    re-checks anyway. A reachable gateway that refuses or errors prints one
    fallback line. Nothing here raises, and nothing changes the exit code: the
    update has already succeeded.
    """
    try:
        port = resolve_client_port(None)
        # The secret must reach only THIS install's gateway: a stale configured
        # port bound by a co-resident local user would otherwise receive the
        # shared local secret -- the live gateway's own accepted credential --
        # which they could replay to mint owner tokens. The proof is bound to
        # the connection that carries it (_owner_verified_secret_urlopen), not
        # checked on one connection and sent on another, which would leave the
        # gateway free to die and a foreign listener free to bind the port in
        # between.
        secret = read_local_secret(port, dial_host=_CLI_LOOPBACK)
        if not secret:
            # No gateway running (or no secret to reach one) -- the next boot
            # re-checks anyway, so there is nothing to reconcile.
            return
        req = urllib.request.Request(
            f"http://{_CLI_LOOPBACK}:{port}/api/update/revalidate",
            method="POST",
            headers={"X-Local-Secret": secret, "Content-Type": "application/json"},
            data=b"{}",
        )
        with _owner_verified_secret_urlopen(
            req, port, timeout=5, operation="gateway_update_revalidate"
        ) as resp:
            if int(resp.status) == 200:
                print("  🔄 Update badge refreshed on the running gateway")
                return
        print("  ℹ️  Could not refresh the update badge; it reconciles on next check")
    except (
        _GatewayPeerRefused,
        http.client.HTTPException,
        OSError,
        ValueError,
        urllib.error.URLError,
    ):
        # Includes a gateway that is not up (connection refused), a peer whose
        # ownership could not be proven on the connection, and any malformed
        # response. The update stands regardless.
        print("  ℹ️  Could not refresh the update badge; it reconciles on next check")


def _refresh_agent_config(proj: str) -> None:
    """Re-install agent config so new denied commands take effect.

    Runs as a subprocess since the current process has old code loaded. The
    refresh is best-effort: the update itself has already succeeded, so any
    failure here downgrades to a warning telling the operator to re-run setup.

    Two hardening properties this call site must keep:

    * ``stdin`` is ``DEVNULL``. With ``capture_output=True`` the child's
      output is piped into a buffer nobody displays until the call returns,
      so any prompt it asks is invisible — and with an inherited terminal it
      would block silently until the timeout. EOF on stdin makes a prompt
      return immediately instead of hanging (``_input_or_skip`` takes its
      ``_SetupAborted`` path, which setup treats as a clean skip; a bare
      ``input()`` gets ``EOFError``), structurally, without relying on every
      prompt in setup to guard itself with an isatty check.
    * ``TimeoutExpired`` is caught. It is raised, not returned, so without a
      handler a slow refresh would traceback out of ``kirocrew update`` right
      after the success banner printed.
    """
    print("  🔒 Refreshing agent config…")
    try:
        r = subprocess.run(
            platform_compat.isolated_python_argv("-P", "-m", "kiro_crew", "setup", "--agent-only"),
            cwd=proj,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        logging.getLogger("kiro_crew.cli_server").warning(
            "agent-only config refresh timed out after %ss; skipping (best-effort)", exc.timeout
        )
        print("  ⚠️  Agent config refresh timed out — run: kirocrew setup --agent-only")
        return
    if r.returncode == 0:
        print("  ✅ Agent config refreshed (deniedCommands + hooks updated)")
    else:
        print("  ⚠️  Agent config refresh failed — run: kirocrew setup --agent-only")


def _update_wheel(layout) -> None:
    """Update a wheel/cli.sh install by checking the release feed and re-running the installer.

    This is the path taken when KIROCREW_PROJECT_DIR is unset or has no .git —
    the standard state for ``curl | sh`` installs where the venv at
    ``~/.kiro/crew-venv`` has no source tree.
    """

    from kiro_crew import __version__ as local_version
    from kiro_crew.platform import wheel_apply
    from kiro_crew.platform.update_layout import (
        non_managed_pip_update_hint,
        release_channel,
        wheel_update_command,
    )
    from kiro_crew.platform.wheel_engine import running_from_managed_venv, running_from_pipx

    channel = release_channel()
    # The one preflight every apply route runs, in its one order: the policy
    # source pin on both CDN bases (a pinned fleet's wheel installs cannot bypass
    # the ceiling), then the shape of the operator-set KIROCREW_CDN_BASE, which
    # flows through wheel_update_command() into ``sh -c``.
    try:
        feed_base, artifact_base = wheel_apply.preflight_bases()
    except wheel_apply.WheelApplyRefused as exc:
        if exc.code == "blocked_by_policy":
            print(f"  🛡️  Update blocked by security policy: {exc.message}")
        else:
            print("  ❌ CDN base URL contains disallowed characters")
        sys.exit(1)
    feed_url = f"{feed_base}/feed/{channel}/latest-cli.json"

    print(f"  📦 Install type: {layout.kind} (channel: {channel})")
    # feed_url embeds the CDN base; a userinfo-bearing KIROCREW_CDN_BASE
    # (user:pass@host) passes cdn_bases_are_safe() and would otherwise land in
    # terminal scrollback. Redact the displayed form; the fetch below still uses
    # the real feed_url.
    from kiro_crew.security import redact_credentials

    shown_feed_url, _ = redact_credentials(feed_url)
    print(f"  📡 Checking {shown_feed_url}…")

    # Fetch the release feed (scheme-validated to satisfy SAST — cdn_bases()
    # already enforces https but Semgrep cannot see through the indirection).
    if not feed_url.startswith("https://"):
        print(f"  ❌ Refusing non-HTTPS feed URL: {shown_feed_url}")
        sys.exit(1)
    try:
        req = urllib.request.Request(feed_url, headers={"User-Agent": "kirocrew-update/1"})
        with urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=15
        ) as resp:
            raw = resp.read(65536 + 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  ❌ Could not reach release feed: {e}")
        print("\n  To update manually, run:")
        print(f"    {wheel_update_command(channel)}")
        sys.exit(1)

    if len(raw) > 65536:
        print("  ❌ Release feed response too large — may be corrupted")
        sys.exit(1)

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        print("  ❌ Release feed is not valid JSON")
        sys.exit(1)

    if not isinstance(manifest, dict):
        print("  ❌ Release feed has unexpected format")
        sys.exit(1)

    if manifest.get("schema") != "kirocrew-cli-artifact-manifest-v1":
        print("  ❌ Release feed schema mismatch — update the installer first")
        print(f"    {wheel_update_command(channel)}")
        sys.exit(1)

    if manifest.get("channel") != channel:
        print(f"  ❌ Feed channel mismatch (expected {channel}, got {manifest.get('channel')})")
        sys.exit(1)

    remote_version = manifest.get("version", "")
    if not remote_version or not isinstance(remote_version, str):
        print("  ❌ No version in release feed")
        sys.exit(1)

    print(f"  📋 Current: {local_version}")
    print(f"  📋 Latest:  {remote_version}")

    # Compare versions using the same logic as the dashboard
    from kiro_crew.dashboard.handlers.updates import _is_newer

    newer = _is_newer(remote_version, local_version)
    if newer is None:
        print("  ⚠️  Could not compare versions — updating anyway to be safe")
    elif not newer:
        print("\n✅ Already on the latest version!")
        return

    # The managed-venv shape takes the shadow path: the new version is built
    # into a fresh sibling tree while this install keeps working, verified,
    # then promoted atomically. The running gateway is never overwritten in
    # place — its restart picks the new tree up through the stable link. Every
    # other shape (pipx, a bare venv the operator manages) keeps the
    # installer re-run, whose behavior is owned by cli.sh.
    if running_from_managed_venv():
        _update_managed_venv(channel, feed_base, artifact_base, remote_version)
        return

    # A plain `pip install` into an environment the user manages (not pipx, not
    # the installer's managed venv) must NOT take the installer re-run: that
    # builds a SECOND copy (a pipx venv, or a managed venv plus a ~/.local/bin
    # symlink) while the environment serving the user keeps the old version, and
    # prints success anyway. pipx installs keep the re-run — the installer owns
    # that venv and upgrades it in place. Refuse with the in-place upgrade hint
    # instead, on every platform (this also replaces the uninformative Windows
    # exit-1 the installer path produced).
    if not running_from_pipx():
        upgrade, restart = non_managed_pip_update_hint()
        from kiro_crew.security import redact_credentials

        print("\n  ⚠️  This looks like a plain `pip install` in an environment you manage.")
        print("  Re-running the installer would add a second copy and leave this one")
        if upgrade.command is not None:
            # The upgrade command embeds the CDN base, and a userinfo-bearing
            # KIROCREW_CDN_BASE (user:pass@host) passes _SAFE_CDN_BASE_RE — so the
            # PRINTED form runs the same credential redaction the gateway sibling
            # applies to its logged form. The command stays runnable; only the
            # displayed credential is stripped.
            printed_upgrade, _ = redact_credentials(upgrade.command)
            print(f"  on {local_version}. Upgrade this environment in place instead:")
            print(f"    {printed_upgrade}")
            print("  then restart the gateway to use the new version:")
            print(f"    {restart}")
        else:
            # The signed wheel could not be fetched/verified, so there is no
            # safe command to hand over — a name-resolving --extra-index-url form
            # would reopen the dependency-confusion vector. Report the failure
            # with retry/manual-install guidance instead. The note carries the
            # channel artifact URL (which embeds the CDN base), so it runs the
            # same credential redaction as the command would.
            printed_note, _ = redact_credentials(upgrade.note)
            print(f"  on {local_version}. The signed upgrade {printed_note}")
            print("  Once upgraded, restart the gateway to use the new version:")
            print(f"    {restart}")
        sys.exit(1)

    # Run the installer
    cmd = wheel_update_command(channel)
    print("\n  🔄 Running installer…")
    print(f"     {cmd}\n")

    # Platform guard: cli.sh is a POSIX shell script; on Windows there is no sh.
    if sys.platform == "win32":
        print("  ❌ Wheel self-update is not supported on Windows.")
        print("  To update manually, run in PowerShell:")
        print(f"    {cmd}")
        sys.exit(1)

    # After the Windows refusal, so a host that cannot self-update never spends a copy.
    _snapshot_memory_or_exit()
    # The installer moves the venv aside and restores it from its TERM trap,
    # so it is never killed outright: it runs in its own session (one group to
    # signal, not ours) and a timeout or Ctrl-C stops it with SIGTERM and a
    # rollback grace before anything is SIGKILLed.
    from kiro_crew.platform.update_provider import INSTALLER_TIMEOUT_TERM_GRACE_SECS

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(["sh", "-c", cmd], start_new_session=True)
        returncode = proc.wait(timeout=300)
    except FileNotFoundError:
        print("  ❌ 'sh' not found — cannot run the installer.")
        print("  To update manually, run:")
        print(f"    {cmd}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                platform_compat.terminate_and_reap_sync(
                    proc, grace=INSTALLER_TIMEOUT_TERM_GRACE_SECS
                )
            except KeyboardInterrupt:
                pass  # the stop already completed; still print the guidance below
        print("\n  ❌ Installer timed out (5 min)")
        print("  Try running manually:")
        print(f"    {cmd}")
        sys.exit(1)
    except KeyboardInterrupt:
        # The installer is outside the terminal's foreground group now, so
        # Ctrl-C reaches only this process; pass the stop on gracefully.
        if proc is not None:
            print("\n  Stopping the installer (it restores the previous install)…")
            try:
                platform_compat.terminate_and_reap_sync(
                    proc, grace=INSTALLER_TIMEOUT_TERM_GRACE_SECS
                )
            except KeyboardInterrupt:
                pass  # a second Ctrl-C during the stop; the stop already completed
        sys.exit(130)
    if returncode != 0:
        print(f"\n  ❌ Installer exited with code {returncode}")
        print("  Try running manually:")
        print(f"    {cmd}")
        sys.exit(1)

    print(f"\n✅ Kiro Crew updated to {remote_version}!")
    print("\n  Restart the gateway to use the new version:")
    print("    kirocrew restart")


def _update_managed_venv(
    channel: str, feed_base: str, artifact_base: str, remote_version: str
) -> None:
    """``kirocrew update`` on the managed venv: the shadow engine, in this shell.

    The new version is built into a fresh sibling tree while this install keeps
    working, verified, then promoted atomically; the running gateway is never
    overwritten in place, and its restart picks the new tree up through the
    stable link. Memory readiness is checked before anything is downloaded and
    the copy taken just before promotion, the same steps every apply route runs.
    Children get THIS shell's full environment (``trusted_env=False``), so a
    toolchain on the operator's PATH reaches a source build. How the apply ended
    is read through the one classification the gateway uses, and the installer
    re-run is offered only where it is a real way forward: never while another
    update holds the lock (it would race that apply's promotion) and never for a
    refused memory copy (it would perform the un-copied update).
    """
    from kiro_crew.platform import wheel_apply
    from kiro_crew.platform.wheel_engine import WheelUpdateError, apply_wheel_update

    print("\n  🔄 Building the new version beside the current one…")

    def _progress(msg: str) -> None:
        print(f"     {msg}")

    failure: BaseException | None = None
    reattach = False
    try:
        # Inside the try: a feed version outside the release grammar is refused here.
        reattach = wheel_apply.userns_reattach_needed(remote_version)
        promoted = apply_wheel_update(
            channel=channel,
            feed_base=feed_base,
            artifact_base=artifact_base,
            expected_version=remote_version,
            progress=_progress,
            preflight=wheel_apply.check_memory_ready,
            before_promote=wheel_apply.memory_snapshot_hook(_progress),
            trusted_env=False,
        )
    except (WheelUpdateError, OSError) as exc:
        # The engine wraps its own I/O failures in WheelUpdateError, but
        # staging-filesystem errors raised outside those conversion sites (a
        # full or unwritable disk at mkdir/tempdir time) surface as raw OSError;
        # both take the operator-facing path instead of a traceback.
        failure = exc
    outcome = wheel_apply.classify(failure)
    if outcome.status == "promoted":
        print(f"\n✅ Kiro Crew {remote_version} installed at {promoted}")
        if reattach:
            print(f"\n  ⚠️  {wheel_apply.userns_reattach_after_apply(remote_version)}")
            return
        print("\n  Restart the gateway to switch to it:")
        print("    kirocrew restart")
        return
    # Redacted: failure text can quote the URL it tried, which may carry
    # credentials from a token-bearing KIROCREW_CDN_BASE.
    message = wheel_apply.shown_failure_text(str(failure), limit=None)
    if outcome.status == "busy":
        print(f"\n  ⏳ {message}")
        print("     Another update (possibly the gateway's own) is in progress. Wait for it")
        print("     to finish, then run `kirocrew update` again.")
        sys.exit(1)
    print(f"\n  ❌ {message}")
    if outcome.status in ("snapshot_failed", "deferred"):
        sys.exit(1)
    if outcome.status == "incompatible":
        print(f"  {wheel_apply.incompatible_remedy(channel)}")
        sys.exit(1)
    print("  The current install was not modified. To update by")
    print("  re-running the installer instead:")
    print(f"    {wheel_apply.installer_rerun_command(channel)}")
    sys.exit(1)
