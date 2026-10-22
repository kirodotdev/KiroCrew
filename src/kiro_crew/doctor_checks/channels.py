"""Channel rows of ``kirocrew doctor``: Slack, Discord, WhatsApp, Signal, and every other one.

No token value is ever printed, in whole or in part, and no phone number either.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig


def _discord_live_state(port: int | None) -> dict[str, object] | None:
    """Read the gateway's live Discord state, or ``None`` when unreachable.

    Loopback only, and only the two liveness fields are ever consumed: the same
    endpoint also returns a masked token preview, which has no business in a
    report an operator pastes into an issue. Unreachable covers every reason
    (gateway down, token auth on this interface, a stale port) because none of
    them is a Discord fault, so all of them read the same to the reader.
    """
    if not port:
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/discord/config")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- loopback host literal plus a fixed internal path; the only interpolated value is the gateway port from config/env, so no scheme or host is reachable from input  # noqa: E501
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _discord_msg_content_line(
    grants: cli_doctor.intent_probe.IntentGrants, *, needs_content: bool, issues: list[str]
) -> None:
    """Report the Message Content intent against what this install needs.

    Severity is decided by the allow-lists, not by the grant alone: Discord
    delivers DM content without the privileged intent, so a DM-only install
    with the intent off is correct, while a thread or channel allow-list with
    the intent off is a channel that silently reads nothing.
    """
    state = grants.message_content
    if not needs_content:
        detail = (
            "on, and unused by a DM-only install"
            if state in cli_doctor.intent_probe.GRANTED_STATES
            else "not needed (DMs deliver content without it)"
        )
        print(f"  msg content: ⏭  {detail}")
    elif state in cli_doctor.intent_probe.GRANTED_STATES:
        limited = state == cli_doctor.intent_probe.INTENT_LIMITED
        extra = " (capped at 100 servers until the app is verified)" if limited else ""
        print(f"  msg content: ✅ granted{extra}")
    elif state == cli_doctor.intent_probe.INTENT_DISABLED:
        print("  msg content: ❌ OFF, so thread and channel messages arrive empty")
        print(f"{render._INDENT}and Discord can close the connection with code 4014.")
        print(f"{render._INDENT}Fix: Developer Portal → Bot → Message Content Intent,")
        print(f"{render._INDENT}then `kirocrew restart`.")
        issues.append("discord: Message Content Intent off with threads allow-listed")
    else:
        print(f"  msg content: ⚠️  cannot verify ({grants.error or 'no answer'})")
        print(f"{render._INDENT}If thread messages arrive empty, enable Message Content")
        print(f"{render._INDENT}Intent in the Developer Portal → Bot.")


def _discord_unused_intent_line(label: str, name: str, state: str) -> None:
    """Flag a privileged intent nothing in Kiro Crew reads, if it is granted.

    Silent when the intent is off (the wanted state) or unknown (the probe
    already reported that once), so this line only ever appears when there is
    something to turn off.
    """
    if state in cli_doctor.intent_probe.GRANTED_STATES:
        print(f"  {label + ':':<13}⚠️  {name} Intent is on but unused")
        print(f"{render._INDENT}Turn it off in the Developer Portal → Bot: nothing in Kiro")
        print(f"{render._INDENT}Crew reads it, and it widens what Discord sends this bot.")


def _discord_install_line(application_id: str, *, dm_only: bool) -> None:
    """Print the install URL matching this configuration, when it can be built.

    Discord has no app manifest to publish, so the authorize URL IS the install
    surface. The app id comes from the live probe; without it (no token, or
    offline) the doc keeps the fallback, since a URL with a placeholder id is
    not something an operator can click.
    """
    shape = "DM-only" if dm_only else "thread-capable"
    try:
        url = cli_doctor.install_url.build_install_url(application_id, dm_only=dm_only)
    except ValueError:
        print(f"  install URL: ⏭  needs the app id: the {shape} template is")
        print(f"{render._INDENT}in the Discord Integration doc")
        return
    print(f"  install URL: {url}")
    print(f"{render._INDENT}({shape}: re-run it to update scopes or permissions)")


def _doctor_discord(
    cfg: KiroCrewConfig, creds: dict[str, str], port: int | None, issues: list[str]
) -> None:
    """Report the Discord channel: config, grants, and the live connection.

    Ordered the way a Discord install fails: the channel must be enabled, then
    hold a token, then allow SOMEONE (an empty user allow-list is a fail-closed
    transport that denies every message, and is the most common way a
    fully-configured install stays mute), then hold the privileged intent its
    allow-lists imply, and only then be connected. Every branch names the
    action that fixes it, because the reader of this section is someone whose
    bot is not answering.
    """
    print("\nDiscord Integration")
    dc = cfg.discord
    if not dc.enabled:
        print("  status:      ⏭  not enabled (optional)")
        print("  setup:       enable it in the dashboard → Settings → Discord, or set")
        print(f"{render._INDENT}discord.enabled in config.json and DISCORD_BOT_TOKEN in")
        print(f"{render._INDENT}{cli_doctor.env_path()}, then `kirocrew restart`")
        return

    print("  status:      ✅ enabled")
    # Same resolution order the gateway uses, so doctor and the running channel
    # can never disagree about whether a token exists. The value itself is
    # never printed, in whole or in part.
    token = creds.get(cli_doctor.CRED_DISCORD_BOT_TOKEN, "") or dc.bot_token
    if token:
        print("  token:       ✅ present")
    else:
        print("  token:       ❌ missing, so the channel never starts")
        print(f"{render._INDENT}Fix: paste the bot token in Settings → Discord, or add")
        print(
            f"{render._INDENT}DISCORD_BOT_TOKEN=<token> to {cli_doctor.env_path()}, then `kirocrew restart`"
        )
        issues.append("discord: enabled without a bot token")

    users = [str(u) for u in dc.allowed_user_ids]
    threads = [str(t) for t in dc.allowed_thread_ids]
    channels = [str(c) for c in dc.allowed_channel_ids]
    if users:
        print(f"  users:       ✅ {len(users)} allow-listed")
    else:
        print("  users:       ❌ allow-list empty, so EVERY message is denied")
        print(f"{render._INDENT}Fix: add your numeric user ID under Settings → Discord")
        print(f"{render._INDENT}(Discord → Settings → Advanced → Developer Mode, then")
        print(f"{render._INDENT}right-click your name → Copy User ID), then `kirocrew restart`")
        issues.append("discord: empty user allow-list denies every message")

    # A server allow-list of either kind is what makes the privileged intent
    # mandatory, so the line that reports the allow-lists names that link: the
    # operator who just added a thread ID is the one who has to go and grant it.
    needs_content = bool(threads or channels)
    if needs_content:
        print(
            f"  servers:     ✅ {len(threads)} thread(s), {len(channels)} channel(s)"
            " (Message Content required)"
        )
    else:
        print("  servers:     ⏹ none, DMs only (add thread or channel IDs to use one)")

    grants = cli_doctor._discord_intent_grants(token)
    _discord_msg_content_line(grants, needs_content=needs_content, issues=issues)
    _discord_unused_intent_line("members", "Server Members", grants.server_members)
    _discord_unused_intent_line("presence", "Presence", grants.presence)

    live = _discord_live_state(port)
    if live is None:
        print("  connection:  ⏹ live state unavailable (gateway not running, or it")
        print(f"{render._INDENT}requires a dashboard token on this interface)")
    elif live.get("connected"):
        print("  connection:  ✅ connected to Discord's Gateway")
    elif str(live.get("connect_error", "")):
        # Foreign text on the way to a terminal: shown escaped, so a control
        # sequence in a close reason cannot rewrite the lines around it.
        reason = render._safe_display(str(live.get("connect_error", ""))[:120])
        print(f"  connection:  ❌ not connected: {reason}")
        print(f"{render._INDENT}Fix: 4014 = enable Message Content Intent (or clear the")
        print(f"{render._INDENT}thread and channel allow-lists); 4004 = reset the bot")
        print(f"{render._INDENT}token. Then `kirocrew restart`.")
        issues.append("discord: channel not connected")
    else:
        print("  connection:  ⚠️  not connected, and no reason was recorded")
        print(f"{render._INDENT}Discord settings are read at startup: run `kirocrew")
        print(f"{render._INDENT}restart` after changing them.")

    _discord_install_line(grants.application_id, dm_only=not needs_content)


def _doctor_whatsapp(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Report the WhatsApp channel's two invisible prerequisites.

    WhatsApp is the only channel whose whole runtime hangs off an OPTIONAL wheel
    plus a locally stored credential, so both halves can be absent on a machine
    whose config says the channel is on. Neither absence produces an error the
    operator sees: a message simply never arrives, which is exactly what a
    preflight exists to answer.

    Both probes are cheap and side-effect free by design. ``neonize_available()``
    is a ``find_spec`` metadata lookup and the store check is one ``stat``; doctor
    must never import neonize (a ~19 MB ``ctypes`` load plus protobuf descriptors)
    or construct a client, because a health check that initializes the subsystem it
    is checking is both slow and a side effect of asking a question.
    """
    # Function-local, so the channel package loads only when this section runs,
    # never merely because the doctor's families were imported.
    from kiro_crew.whatsapp.client import (
        MISSING_EXTRA_HINT,
        default_db_path,
        neonize_available,
    )

    print("\nWhatsApp Integration")
    wa = cfg.whatsapp
    if not wa.enabled:
        print("  status:      ⏭  not enabled (optional)")
        print("  setup:       run 'kirocrew setup --whatsapp', or enable it from")
        print("               the dashboard (Settings → Messaging Channels → WhatsApp)")
        return

    if neonize_available():
        print("  extra:       ✅ neonize importable")
    else:
        print("  extra:       ❌ not installed, so the enabled channel cannot start")
        print(f"               Fix: {MISSING_EXTRA_HINT}")
        issues.append("whatsapp extra missing")

    # The SAME expression ``whatsapp/gateway.py`` builds the client from, so doctor
    # can never report on a store the channel does not open. ``data_home()``
    # rather than ``config_dir()``: this is a read, and it must not refresh the
    # recovery breadcrumb as a side effect of reporting a path.
    store = default_db_path(cli_doctor.data_home())
    if store.exists():
        print(f"  session:     ✅ paired session store at {store}")
    else:
        # Deliberately NOT an issue. Pairing is a QR scan served BY the running
        # gateway, so a freshly enabled channel legitimately has no store yet, and
        # failing here would break the documented `kirocrew doctor && kirocrew
        # gateway` chain at the one moment the operator must start the gateway to
        # make progress.
        print("  session:     ⚠️  not paired yet, so the channel starts unpaired")
        print(f"               Expected store: {store}")
        print("               Pair from the dashboard (Settings → Messaging Channels → WhatsApp)")

    groups = [g for g in (wa.groups or []) if isinstance(g, dict) and str(g.get("jid", "")).strip()]
    if groups:
        # Membership is only knowable from a live connection, so the gateway checks
        # it on connect and logs the unmatched JIDs; doctor reports the count.
        print(f"  groups:      ✅ {len(groups)} configured")
    else:
        print("  groups:      ⏹ none configured (group messages are ignored)")
    print(f"  dm policy:   {wa.dm_policy}")


#: Bound on ``signal-cli --version``. A JVM build takes a second or two to start;
#: a doctor run must never hang on a wedged binary, so past this it is reported
#: as unresponsive rather than waited on.
SIGNAL_VERSION_TIMEOUT_S = 15.0

#: The version probe's process runner, bound here so a test can inject a result
#: without patching ``subprocess.run`` process-wide or needing a real binary.
_run = subprocess.run


def _signal_needs_java(cli_path: str) -> bool:
    """Whether *cli_path* is the JVM launcher script rather than the native build.

    The upstream JVM distribution ships ``bin/signal-cli`` as a shell script
    (``#!``) that starts ``java``; the Linux native build is one compiled
    executable. Two bytes answer it without running anything.
    """
    try:
        with open(cli_path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _java_location() -> str:
    """The ``java`` the signal-cli launcher would pick, or ``""``.

    The launcher script prefers ``$JAVA_HOME/bin/java`` and falls back to
    ``java`` on ``PATH``; this follows the same order so the answer matches what
    the gateway's child would run under the same environment.
    """
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("java") or ""


def _signal_cli_version(cli_path: str) -> tuple[str, str]:
    """``(version, problem)`` from ``signal-cli --version``; exactly one is set.

    Bounded by :data:`SIGNAL_VERSION_TIMEOUT_S` and run with no stdin, so it can
    neither hang nor wait on input. ``--version`` opens no account and touches
    no network.
    """
    try:
        proc = _run(
            [cli_path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SIGNAL_VERSION_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", f"no answer within {SIGNAL_VERSION_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return "", f"could not run it ({exc.strerror or type(exc).__name__})"
    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        return out.splitlines()[0][:120], ""
    lines = [line for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
    reason = lines[-1].strip()[:120] if lines else "no output"
    return "", f"exited {proc.returncode}: {reason}"


def _signal_data_dir() -> Path:
    """signal-cli's default data directory, resolved the way signal-cli does.

    ``$XDG_DATA_HOME/signal-cli``, else ``~/.local/share/signal-cli``. The
    gateway passes no ``--data-dir``, so this is the store the channel opens.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "signal-cli"


def _signal_local_accounts(data_dir: Path) -> set[str] | None:
    """Every number and ACI signal-cli's local ``accounts.json`` lists.

    ``None`` when there is no readable store. One file read: no subprocess, no
    network, and no contention for the account lock a running gateway holds.
    """
    from kiro_crew.signal_cli.client import normalize_identifier

    try:
        raw = json.loads((data_dir / "data" / "accounts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = raw.get("accounts") if isinstance(raw, dict) else None
    known: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            for key in ("number", "uuid"):
                if ident := normalize_identifier(entry.get(key)):
                    known.add(ident)
    return known


def _doctor_signal(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Report the Signal channel's prerequisites without starting the gateway.

    Everything the channel needs lives outside Kiro Crew -- a signal-cli binary,
    a Java runtime for its JVM build, an account registered or linked out of
    band -- and each one fails as silence: the gateway retries a failed start in
    the background, and the fail-closed allow-list drops messages unanswered.

    The binary comes from the SAME resolver the channel spawns from
    (``signal_cli/bridge_path.py``), so doctor cannot vouch for a binary the
    gateway would not run. Every identifier printed goes through
    ``redact_identifier``: a phone number is personal data.
    """
    # Function-local, so the channel package loads only when this section runs.
    from kiro_crew.signal_cli.bridge_path import (
        ENV_OVERRIDE,
        describe_lookup,
        resolve_bridge_path,
    )
    from kiro_crew.signal_cli.client import (
        account_identifier,
        coerce_identifier,
        identifier_problem,
        normalize_identifier,
        redact_identifier,
    )
    from kiro_crew.signal_cli.transport import allowed_numbers_from_config

    print("\nSignal Integration")
    section = getattr(cfg, "signal", None)
    if section is None or not getattr(section, "enabled", False):
        print("  status:      ⏭  not enabled (optional)")
        print("  setup:       install signal-cli, register or link it, then set")
        print("               signal.enabled, signal.account and signal.allowed_numbers")
        return

    cli_path = resolve_bridge_path()
    if cli_path:
        print(f"  signal-cli:  ✅ {cli_path}")
        version, problem = _signal_cli_version(cli_path)
        if version:
            print(f"  version:     ✅ {version}")
        else:
            print(f"  version:     ❌ {problem}")
            print("               The channel cannot start until signal-cli runs.")
            issues.append("signal: signal-cli does not run")
        if _signal_needs_java(cli_path):
            java = _java_location()
            if java:
                print(f"  java:        ✅ {java} (JVM build)")
            else:
                print("  java:        ❌ not found, and this signal-cli is the JVM build")
                print("               Fix: install the Java its release names, set JAVA_HOME,")
                print("               or use the Linux native build")
                issues.append("signal: java missing")
    else:
        print("  signal-cli:  ❌ not found, so the enabled channel cannot start")
        print(f"               {describe_lookup()}")
        print(f"               Fix: install signal-cli, or set {ENV_OVERRIDE} to its absolute path")
        issues.append("signal: signal-cli not found")

    raw_account = getattr(section, "account", "")
    account = account_identifier(raw_account)
    account_problem = identifier_problem(raw_account)
    if not account:
        print("  account:     ❌ signal.account is not set")
        issues.append("signal: account not set")
    elif account_problem:
        print(f"  account:     ❌ {redact_identifier(account)} is {account_problem}")
        issues.append("signal: account malformed")
    else:
        print(f"  account:     ✅ {redact_identifier(account)}")
        data_dir = _signal_data_dir()
        known = _signal_local_accounts(data_dir)
        # Warnings, not issues: a store under another user's home or a custom
        # data dir is legitimate, and signal-cli is the final word at start.
        if known is None:
            print(f"  store:       ⚠️  no signal-cli account store at {data_dir}")
            print("               Register or link the account first (see signal-integration.md)")
        elif account in known:
            print("  store:       ✅ account found in signal-cli's local store")
        else:
            print(f"  store:       ⚠️  account not listed in {data_dir / 'data' / 'accounts.json'}")
            print("               Register or link it first, or signal-cli rejects it at start")

    raw_allowed = getattr(section, "allowed_numbers", None)
    entries = raw_allowed if isinstance(raw_allowed, list) else []
    bad = [e for e in entries if identifier_problem(e) not in ("", "empty")]
    usable = [
        n for n in allowed_numbers_from_config(raw_allowed) or [] if not identifier_problem(n)
    ]
    if usable:
        print(
            f"  allowed:     ✅ {len(usable)} usable {'entry' if len(usable) == 1 else 'entries'}"
        )
    else:
        print("  allowed:     ❌ no usable allowed_numbers, so every message is rejected")
        print("               Add your own number or ACI (for Note to Self, the account's own)")
        issues.append("signal: allowed_numbers empty")
    if bad:
        shown = ", ".join(
            redact_identifier(normalize_identifier(coerce_identifier(e)) or str(e)) for e in bad
        )
        print(f"               ❌ {len(bad)} never match (not +E.164 or an ACI): {shown}")
        issues.append("signal: malformed allowed_numbers")


def _doctor_slack(
    cfg: KiroCrewConfig, creds: dict[str, str], has_slack: bool, issues: list[str]
) -> None:
    """Render the ``Slack Integration`` section.

    *has_slack* is the Configuration section's own token check, so the two
    sections cannot disagree about whether Slack is configured.
    """
    print("\nSlack Integration")
    if has_slack:
        has_owner = bool(creds.get("KIROCREW_OWNER_ID"))
        print("  tokens:      ✅ configured")
        if has_owner:
            print(f"  owner:       ✅ {creds['KIROCREW_OWNER_ID']}")
        else:
            print("  owner:       ⚠️  KIROCREW_OWNER_ID not set")

        # Optional workspace allowlist validation (default-open unless the user
        # configured slack.allowed_enterprise_ids).
        bot_token = creds.get("SLACK_BOT_TOKEN", "")
        if bot_token:
            extra_ids = cfg.slack_enterprise_ids
            # Route through the active PlatformContext's Slack gate so the doctor
            # reports the SAME enterprise-gate decision the gateway enforces
            # (slack/events.py uses the context gate). The Default gate delegates
            # to enterprise.validate_enterprise, so standalone is unchanged.
            if cli_doctor.current_context().slack_gate.validate_enterprise(
                bot_token, extra_ids=extra_ids
            ):
                print("  workspace:   ✅ allowed")
            else:
                print("  workspace:   ❌ not in configured workspace allowlist")
                print("               The gateway will refuse to connect.")
                issues.append("slack workspace: not in allowlist")
    else:
        print("  status:      ⏭  not configured (optional)")
        print("  setup:       run 'kirocrew setup --slack', or connect any channel")
        print("               (Slack, Discord, Telegram, …) from the dashboard")


def _doctor_other_channels(cfg: KiroCrewConfig, creds: dict[str, str], issues: list[str]) -> None:
    """Render the ``Other Channels`` section: every channel without its own section.

    One loop over the roster rather than a section per channel: the doctor knows
    Slack and Discord by name, so without this an operator with
    `telegram.enabled: true` and no token gets a clean bill of health from the
    tool whose whole job is telling them what is wrong. Readiness is derived from
    descriptor data, so the next channel is covered by adding its descriptor.
    """
    print("\nOther Channels")
    try:
        from kiro_crew.channels import channel_readiness

        # Slack, Discord, WhatsApp and Signal each have a dedicated section above
        # reporting the same prerequisites, so listing them again here would name
        # one fault twice in the closing issue line.
        rows = [
            row
            for row in channel_readiness(cfg, creds)
            if row.channel_type not in ("slack", "discord", "whatsapp", "signal")
        ]
    except Exception:
        rows = []
        print("  status:      ⚠️  channel roster unavailable")
    if rows and not any(row.enabled for row in rows):
        print("  status:      ⏭  none enabled (optional)")
        print("  setup:       connect one from the dashboard's Settings > Messaging Channels")
    for row in rows:
        if not row.enabled:
            continue
        name = row.channel_type
        if row.ready:
            print(f"  {name + ':':12} ✅ enabled, credentials present")
        else:
            # Credentials and required config are reported separately because they
            # live in different places: a secret belongs in .env, a non-secret like
            # an account id in config.json. One combined line would send the
            # operator to the wrong file.
            parts = []
            if row.missing_credentials:
                parts.append(", ".join(row.missing_credentials))
            if row.missing_config:
                parts.append(", ".join(f"{name}.{attr}" for attr in row.missing_config))
            missing = " and ".join(parts)
            print(f"  {name + ':':12} ❌ enabled but missing {missing}")
            print(
                "               The channel will not start. Set it in "
                "Settings > Messaging Channels, or in ~/.kiro/crew/.env"
            )
            issues.append(f"{name}: missing {missing}")
