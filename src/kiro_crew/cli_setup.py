"""CLI setup subcommand — interactive config wizard (channel credentials are opt-in)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

from kiro_crew import platform_compat, slack_manifest
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cli_chat import _ensure_default_agent_in_config
from kiro_crew.conductor_skill import generate_conductor_skill
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    _WORKSPACE_DIR_NAME,
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    ConfigReadError,
    _default_workspace_base,
    _workspace_dir_file,
    config_path,
    env_path,
    unsandboxed_exec_declared,
    unsandboxed_exec_platform_default,
    update_config_locked,
)
from kiro_crew.constants import DATA_WARNING, MIN_NODE_MAJOR
from kiro_crew.sandbox import unavailable_kind
from kiro_crew.secrets.migrate import _env_lock_path
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.validation import USER_ID_RE


def _get_alias() -> str:
    """Return the user's login name (used to name the Slack app)."""
    alias = os.environ.get("USER") or ""
    if not alias:
        try:
            alias = os.getlogin()
        except OSError:
            pass
    if not alias:
        alias = input("  Your username (e.g. johndoe): ").strip()
    if not alias:
        print(
            "❌ Cannot determine username. Set $USER or re-run with "
            "`kirocrew manifest --alias <alias>`.",
            file=sys.stderr,
        )
        sys.exit(1)
    return alias


def _manifest(alias: str | None = None, output: str | None = None, url: bool = False) -> None:
    """Render slack-manifest.yaml with the user's alias substituted."""

    alias = alias or _get_alias()
    if not slack_manifest.valid_alias(alias):
        print(
            "❌ Invalid alias — must be alphanumeric, hyphens, or underscores only, "
            f"at most {slack_manifest.ALIAS_MAX} characters.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        rendered = slack_manifest.render(alias)
    except FileNotFoundError:
        print("❌ Cannot find slack-manifest.yaml", file=sys.stderr)
        sys.exit(1)
    if url:
        print("\n🔗 Click to create your Slack app:\n")
        print(f"{slack_manifest.deep_link(alias)}\n")
    elif output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        print(f"✅ Manifest written to {output} (name: KiroCrew-{alias})")
    else:
        print(rendered)


def _fix_shell_profiles() -> None:
    """Remove stale KiroCrew PATH entries from shell profiles."""
    home = Path.home()
    profiles = [
        home / ".zshrc",
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
    ]
    stale_markers = [
        ".kirocrew-app",
    ]
    cleaned_profiles: list[str] = []
    for profile in profiles:
        if not profile.is_file():
            continue
        try:
            lines = profile.read_text(encoding="utf-8").splitlines(keepends=True)
            cleaned = []
            removed = False
            for line in lines:
                if any(m in line for m in stale_markers) and "PATH" in line:
                    removed = True
                    continue
                cleaned.append(line)
            if removed:
                profile.write_text("".join(cleaned), encoding="utf-8")
                print(f"  🔧 Cleaned stale Kiro Crew PATH from {profile.name}")
                cleaned_profiles.append(profile.name)
        except OSError:
            pass
    if cleaned_profiles:
        sources = " or ".join(f"`source ~/{p}`" for p in cleaned_profiles)
        print(f"  ⚠️  Run {sources} or open a new terminal for PATH changes to take effect.")


def _ensure_prerequisites() -> bool:
    """Report on optional prerequisites resolved from PATH.

    The public build's agent backend is ``kiro-cli``. This performs no installs
    and never blocks setup — it only prints guidance for tooling that is missing
    from PATH. Always returns True so setup proceeds.
    """
    header_printed = False

    def _header() -> None:
        nonlocal header_printed
        if not header_printed:
            print("── Prerequisites ──\n")
            header_printed = True

    # Node is required for the dashboard build (and for MCP servers shipped as
    # npm packages, e.g. the Playwright browser MCP).
    if not shutil.which("node"):
        _header()
        print(
            f"  ⚠️  node not found on PATH — install Node.js >= {MIN_NODE_MAJOR} from https://nodejs.org\n"
        )

    # kiro-cli is the agent backend. Note its absence so the user can install it.
    if not shutil.which(KIRO_CLI_BIN):
        _header()
        print(
            "  ℹ️  kiro-cli not found on PATH — install it (the agent backend) "
            "and run 'kiro-cli login'.\n"
        )

    return True


def _find_electron_dir() -> Path | None:
    """Locate the in-repo electron app sources (website/electron).

    The pip-installed package does NOT ship the desktop-app sources, so this
    only resolves from a source checkout: first via KIROCREW_PROJECT_DIR, then
    by walking up from this module's location and the cwd.
    """
    candidates = []
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        candidates.append(Path(proj))
    for base in (Path(__file__).resolve(), Path.cwd()):
        for parent in [base] + list(base.parents):
            candidates.append(parent)
    seen = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        electron_dir = root / "website" / "electron"
        if electron_dir.is_dir():
            return electron_dir
    return None


def _setup_electron() -> None:
    """Build and install the KiroCrew desktop app (macOS only)."""
    if platform.system() != "Darwin":
        print("  ⚠️  Kiro Crew desktop app is only available on macOS.")
        return

    if not shutil.which("node"):
        print("  ❌ Node.js not found — required to build the desktop app.")
        print("     Install Node.js and re-run: kirocrew setup --electron-only")
        return

    electron_dir = _find_electron_dir()
    if electron_dir is None:
        print("  ❌ Desktop app sources not found.")
        print(
            "     The desktop app is built from a source checkout — clone the "
            "repo and run `make desktop` (or run setup from the checkout)."
        )
        return

    print("  🔨 Building Kiro Crew desktop app…")
    npm_install = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=str(electron_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if npm_install.returncode != 0:
        print(f"  ❌ npm install failed: {npm_install.stderr.strip()[:200]}")
        return

    build = subprocess.run(
        ["npx", "electron-builder", "--mac", "--dir"],
        cwd=str(electron_dir),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if build.returncode != 0:
        print(f"  ❌ Electron build failed: {build.stderr.strip()[:200]}")
        return

    arch = "mac-arm64" if platform.machine() == "arm64" else "mac"
    app_src = electron_dir / "dist" / arch / "KiroCrew.app"
    if not app_src.is_dir():
        for candidate in ("mac-arm64", "mac", "mac-x64"):
            app_src = electron_dir / "dist" / candidate / "KiroCrew.app"
            if app_src.is_dir():
                break
    if not app_src.is_dir():
        print("  ❌ Build succeeded but KiroCrew.app not found in dist/")
        return

    app_dest = Path.home() / "Applications" / "KiroCrew.app"
    app_dest.parent.mkdir(parents=True, exist_ok=True)
    if app_dest.is_dir():
        shutil.rmtree(app_dest)
    shutil.copytree(str(app_src), str(app_dest))
    print("  ✅ KiroCrew.app installed to ~/Applications")
    print("     Launch via Spotlight (⌘+Space → KiroCrew) or Finder → ~/Applications")


def _setup(
    agent_only: bool = False,
    electron_only: bool = False,
    clean: bool = False,
    slack: bool = False,
    whatsapp: bool = False,
) -> None:
    """Install agent config and optionally configure credentials."""
    try:
        _setup_impl(
            agent_only=agent_only,
            electron_only=electron_only,
            clean=clean,
            slack=slack,
            whatsapp=whatsapp,
        )
    except _SetupAborted as exc:
        # A closed/piped stdin mid-wizard. One clean line instead of a stack
        # trace at whichever prompt hit it first — every guarded prompt raises,
        # so the exit point is deterministic. Bare `input()` calls still exist
        # in some steps and traceback the old way; converting them all is a
        # sweep for its own change.
        print(f"\n⏭  Setup aborted: {exc}. Re-run interactively to finish.")


def _setup_impl(
    agent_only: bool = False,
    electron_only: bool = False,
    clean: bool = False,
    slack: bool = False,
    whatsapp: bool = False,
) -> None:
    from kiro_crew.agent import install_agent  # circular import: agent imports cli
    from kiro_crew.cli import _project_dir_file  # circular import: cli -> cli_setup -> cli

    if electron_only:
        print("── Desktop App ──\n")
        _setup_electron()
        return

    print("Kiro Crew Setup 👻\n")
    print(f"  {DATA_WARNING.replace(chr(10), chr(10) + '  ')}\n")

    # Report on optional prerequisites.
    _ensure_prerequisites()

    # 0. Save project dir so kirocrew works from anywhere
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        _project_dir_file().parent.mkdir(parents=True, exist_ok=True)
        _project_dir_file().write_text(proj + "\n", encoding="utf-8")
        print(f"  ✅ Project dir saved: {proj}")

    # 1. Choose workspace directory (skip for agent-only — not relevant)
    if not agent_only:
        _setup_workspace_dir()

    # 2. Install the agent config
    print("Installing agent config...")
    agent_path = install_agent(clean=clean)
    print(f"  ✅ Agent installed: {agent_path}")

    # 2a. Ensure `kirocrew` is reachable on PATH (the packaged Electron app
    #     doesn't run install.sh) and purge stale predecessor MCP entries left
    #     in the user's global provider config by the rename from the upstream
    #     project. Both run only here, on the explicit setup/migration path.
    from kiro_crew.agent import ensure_kirocrew_on_path
    from kiro_crew.mcp_cleanup import clean_stale_managed_mcp

    # `claim_existing`: this is the explicit setup path, so the user has named
    # THIS install as the one they want `kirocrew` to mean. Gateway startup
    # deliberately does not, so a background start never takes the command away
    # from a working install (see ensure_kirocrew_on_path).
    shim = ensure_kirocrew_on_path(claim_existing=True)
    if shim:
        print(f"  ✅ Linked kirocrew on PATH: {shim}")
    removed_mcp = clean_stale_managed_mcp()
    if removed_mcp:
        print(f"  ✅ Removed stale MCP entries: {', '.join(removed_mcp)}")

    # 2b. Ensure config.json has default KiroCrew agent for fresh installs
    _ensure_default_agent_in_config()

    # 2c. Generate conductor skill if enabled (agent delegation).
    try:
        cfg = KiroCrewConfig.load()
        if cfg.agent.conductor_skill:
            generate_conductor_skill(SkillsLoader())
            print("  ✅ Conductor skill generated")
        else:
            # Clean up stale skill if previously enabled then disabled.
            skill_path = SkillsLoader()._dir / "conductor" / "SKILL.md"
            if skill_path.exists():
                skill_path.unlink()
    except Exception as exc:
        print(f"  ⚠️  Conductor skill generation failed: {exc}")

    # 2d. Offer the unconfined-exec opt-in on a host with no sandbox backend.
    #     Runs BEFORE the agent-only early return: the servers this unblocks are
    #     the ones install_agent() just wrote, so `--agent-only` needs it too.
    _setup_sandbox_consent()

    if agent_only:
        # --agent-only returns before the channel steps below, so an explicit
        # channel flag has nothing to act on. Say so instead of dropping it
        # silently, and name each flag the caller actually passed.
        requested = (("--slack", slack), ("--whatsapp", whatsapp))
        for flag in [name for name, on in requested if on]:
            print(
                f"\n  ⚠️  {flag} is ignored with --agent-only. Run "
                f"'kirocrew setup {flag}' for its guided setup."
            )
        print("\n👻 Done! Try: kirocrew gateway")
        return

    # 3. Messaging channels (optional, configured after setup by default).
    #    Channel prompts run only on explicit opt-in (`kirocrew setup --slack`,
    #    `kirocrew setup --whatsapp`); the dashboard and CLI need no channel
    #    credentials, and every channel (Slack, Discord, Telegram, Teams, Webex,
    #    WeCom, WeChat, WhatsApp, iMessage) can be connected later from the
    #    dashboard or its setup guide.
    if slack or whatsapp:
        if slack:
            _setup_slack_tokens()

            # 3b. Slash command name (Slack-only concept)
            _setup_slash_command()
        if whatsapp:
            _setup_whatsapp()
    else:
        print("── Messaging Channels ──\n")
        print("  The dashboard works without any messaging credentials.")
        print("  Connect Slack, Discord, Telegram, Teams, Webex, WeCom, WeChat,")
        print("  WhatsApp, or iMessage (macOS only)")
        print("  later from the dashboard (Settings → Channels) or run")
        print("  'kirocrew setup --slack' or 'kirocrew setup --whatsapp' for a")
        print("  guided setup.\n")

    # 4. Timezone
    _setup_timezone()

    # 5. Dashboard URL (remote access)
    _maybe_setup_dashboard_url()

    _maybe_setup_custom_domain()

    # 6. Desktop app (macOS only)
    if platform.system() == "Darwin":
        print("── Desktop App ──\n")
        answer = input("  Install KiroCrew desktop app to ~/Applications? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            _setup_electron()
        else:
            print("  ⏭  Skipped. Install later: kirocrew setup --electron-only\n")

    # 7. Cloud (run KiroCrew on the user's own AWS EC2) — optional, delegated.
    _maybe_setup_cloud()

    print("\n👻 Done! Try: kirocrew doctor && kirocrew gateway")


def _maybe_setup_cloud() -> None:
    """Offer to launch KiroCrew on the user's own AWS EC2.

    A thin delegating step — all AWS/CloudFormation/SSM logic lives in the
    testable ``kiro_crew.cloud`` module. This just asks and hands off to the
    launcher wizard (``kirocrew cloud launch``).
    """
    print("\n── Run on AWS (optional) ──\n")
    print("  Kiro Crew can run 24/7 on your own AWS EC2 instance (bring your own")
    print("  AWS account; credentials stay in the aws CLI — never stored here).")
    try:
        answer = input("  Launch KiroCrew on AWS now? [y/N]: ").strip().lower()
    except (EOFError, UnicodeDecodeError):
        # Piped/non-interactive setup or a non-UTF-8 locale (e.g. C/POSIX on
        # Amazon Linux Cloud Desktop) — input() decodes stdin with the locale
        # encoding and can raise UnicodeDecodeError before it ever returns a
        # string. Treat it like EOF: no usable answer, so take the default.
        answer = ""
    if answer not in ("y", "yes"):
        print("  ⏭  Skipped. Launch later: kirocrew cloud launch\n")
        return
    try:
        import argparse

        from kiro_crew.cli_cloud import handle_cloud

        # hold_tunnel=False: setup still has steps to print after this one, so
        # the wizard must return instead of blocking on the SSM tunnel.
        args = argparse.Namespace(
            cloud_action="launch",
            profile="",
            region="",
            size="",
            yes=False,
            hold_tunnel=False,
        )
        handle_cloud(args)
    except Exception as exc:  # pragma: no cover - non-fatal, informative
        print(f"  Cloud launch could not start: {exc}")
        print("  Run it directly: kirocrew cloud launch\n")


def _setup_workspace_dir() -> None:
    """Prompt user for workspace directory, falling back to platform default."""
    platform_default = _default_workspace_base() / _WORKSPACE_DIR_NAME
    default = platform_default
    label = "Default"
    if _workspace_dir_file().is_file():
        configured = _workspace_dir_file().read_text(encoding="utf-8").strip()
        if configured:
            default = Path(configured)
            label = "Configured"
    print("── Workspace Directory ──\n")
    print("  LLM sessions and task output are stored in a workspace directory.")
    print(f"  {label}: {default}\n")
    # EOF (piped / closed stdin) keeps the default rather than raising a
    # traceback out of the wizard — this step runs FIRST, so a bare input() here
    # made `kirocrew setup < /dev/null` fail before any later guard could help.
    answer = _input_or_skip(f"  Workspace path [{default}]: ") or ""
    chosen = default if answer.lower() in ("", "y", "yes") else Path(answer).expanduser()
    try:
        chosen.mkdir(parents=True, exist_ok=True)
        _workspace_dir_file().parent.mkdir(parents=True, exist_ok=True)
        _workspace_dir_file().write_text(str(chosen) + "\n", encoding="utf-8")
        print(f"  ✅ Workspace: {chosen}\n")
    except OSError as e:
        print(f"  ❌ Cannot create {chosen}: {e}")
        print(f"  Falling back to platform default: {platform_default}\n")


#: Per-call timeout, in seconds, for the Slack API round-trips that check a
#: pasted credential. Mirrors the dashboard credential handler's
#: ``_TOKEN_VERIFY_TIMEOUT``: long enough for a slow corporate proxy, short
#: enough that an unreachable Slack does not read as a hung wizard.
_SLACK_VERIFY_TIMEOUT = 8

#: How many times a value Slack REJECTED is re-asked before the save is
#: abandoned. Only ever more than one at a terminal (see
#: :func:`_stdio_is_interactive`).
_SLACK_VERIFY_ATTEMPTS = 3

#: The only ``users.info`` errors that indict the pasted member ID itself.
#: Every other Slack error indicts the CHECK — a missing ``users:read`` scope,
#: a rate limit, a bot token that has since been revoked — and must degrade to
#: "unverifiable", because refusing an operator's own member ID over a scope
#: they never granted is worse than not checking it.
_SLACK_OWNER_REJECTIONS = frozenset({"user_not_found", "users_not_found"})


def _stdio_is_interactive() -> bool:
    """True when both ends of stdio are a real terminal.

    The wizard may only stop to re-ask a question, or to ask for consent, when
    a human is there to see it: ``kirocrew update`` re-runs setup with its
    output captured and stdin on ``/dev/null``, and a question asked there is a
    hang, not a correction. Same two conditions as ``cli_chat._can_prompt``.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _slack_error_code(exc: Exception) -> str:
    """Slack's own error code from a ``SlackApiError``, bounded and safe.

    Slack's codes (``invalid_auth``, ``not_allowed_token_type``, …) are the
    actionable half of a rejection, so they are surfaced verbatim; anything
    unexpected in the response degrades to a generic label rather than raising
    out of the check.
    """
    try:
        return str(getattr(exc, "response", {}).get("error", "") or "rejected")[:60]
    except Exception:
        return "rejected"


def _verify_slack_secret(key: str, token: str) -> tuple[bool | None, str]:
    """Ask Slack whether it accepts a pasted token, BEFORE it is stored.

    Returns ``(True, detail)`` when Slack accepts it — ``detail`` is the
    workspace name for a bot token and ``""`` otherwise; ``(False, error)``
    when Slack rejects it, carrying Slack's own code; and ``(None, reason)``
    when Slack could not be reached at all. Unverifiable is NOT invalid: being
    offline must never cost the operator the credentials they just typed, so
    callers keep the value on that verdict.

    Same two calls, and the same contract, as the dashboard's credential save
    (``dashboard/handlers/messaging.py::_validate_slack_token``): app-level
    tokens are checked with ``apps.connections.open`` — the call the gateway
    itself makes at startup, so a token that passes here will connect at boot —
    and everything else with ``auth.test``.

    """
    client = WebClient(token=token, timeout=_SLACK_VERIFY_TIMEOUT)
    try:
        if key == CRED_SLACK_APP_TOKEN:
            client.apps_connections_open(app_token=token)
            return True, ""
        resp = client.auth_test()
    except SlackApiError as exc:
        return False, _slack_error_code(exc)
    except Exception as exc:
        # Transport, DNS, proxy, TLS — Slack never answered, so the token is
        # unjudged rather than invalid.
        return None, type(exc).__name__
    return True, str(resp.get("team") or resp.get("team_id") or "")


def _verify_slack_owner_id(bot_token: str, owner_id: str) -> tuple[bool | None, str]:
    """Check a pasted Slack member ID, with the same verdict triple.

    Two independent checks. The FORMAT check catches the common paste error and
    needs no network: a channel ID (``C…``) or a bot ID (``B…``) is not a member
    ID. It uses ``validation.USER_ID_RE``, the same regex the dashboard's Slack
    save enforces, so the two surfaces cannot disagree about what a member ID
    is — and it admits the ``W…`` ids Enterprise Grid issues. Then
    ``users.info`` confirms the ID exists in the workspace the bot token belongs
    to and names its owner, so a well-formed but WRONG id is visible too.

    Only :data:`_SLACK_OWNER_REJECTIONS` indicts the id; every other Slack error
    degrades to unverifiable.
    """
    if not USER_ID_RE.match(owner_id):
        return False, (
            "not a member ID — 'C…' is a channel and 'B…' is a bot; yours starts "
            "with U (or W on Enterprise Grid), from your profile → ⋯ More → Copy member ID"
        )
    client = WebClient(token=bot_token, timeout=_SLACK_VERIFY_TIMEOUT)
    try:
        resp = client.users_info(user=owner_id)
    except SlackApiError as exc:
        code = _slack_error_code(exc)
        if code in _SLACK_OWNER_REJECTIONS:
            return False, f"{code} — no such member in this workspace"
        return None, code
    except Exception as exc:
        return None, type(exc).__name__
    user = resp.get("user") or {}
    return True, str(user.get("real_name") or user.get("name") or "")


def _prompt_verified_slack_value(
    prompt: str,
    current: str,
    noun: str,
    check: Callable[[str], tuple[bool | None, str]],
) -> tuple[str | None, str]:
    """Prompt for one Slack value and hand back Slack's verdict on it.

    Returns ``(value, detail)`` for a value the caller should save — accepted or
    unverifiable — and ``(None, "")`` when Slack rejected every attempt, which the
    caller turns into "nothing was saved". No detail on that leg: each rejection
    code was already printed to the operator as it happened.

    The check runs ONLY at a terminal. Its whole purpose is to put a verdict in
    front of the person who just pasted the value so they can retype it, and
    off a terminal there is neither: nobody sees the verdict, and re-asking
    would consume the NEXT line of a piped answer file and misassign every
    remaining answer in the wizard. So a non-interactive run — ``kirocrew
    update`` re-runs setup with its output captured and stdin on ``/dev/null``
    — stays exactly as it was before this check existed, rather than paying a
    Slack round-trip (and an 8s stall on a host with no egress) for an outcome
    that cannot change. ``kirocrew doctor`` reports on the stored tokens.
    """
    if not _stdio_is_interactive():
        return input(prompt).strip() or current, ""
    for _attempt in range(_SLACK_VERIFY_ATTEMPTS):
        value = input(prompt).strip() or current
        if not value:
            # Nothing typed and nothing configured — the caller reports the
            # missing credential; there is no value to judge.
            return "", ""
        verdict, detail = check(value)
        if verdict is None:
            print(f"  ⚠️  Could not reach Slack to check the {noun} ({detail}) — saved as typed.")
            return value, ""
        if verdict:
            return value, detail
        print(f"  ❌ Slack rejected the {noun}: {detail}")
    return None, ""


def _setup_slack_tokens() -> None:
    """Prompt for Slack tokens and owner ID, write to config_dir/.env."""
    cred_path = env_path()
    existing: dict[str, str] = {}
    if cred_path.exists():
        for line in cred_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    print("── Slack Credentials ──\n")
    print("  See docs/guides/slack-setup.md for how to create a Slack app.\n")

    answer = input("  Configure Slack tokens? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        print("  ⏭  Skipped. Slack integration will be disabled.\n")
        return

    def _mask(val: str) -> str:
        return val[:8] + "…" if len(val) > 12 else val

    cur_app = existing.get(CRED_SLACK_APP_TOKEN, "")
    cur_bot = existing.get(CRED_SLACK_BOT_TOKEN, "")
    cur_owner = existing.get(CRED_OWNER_ID, "")

    hint_app = f" [{_mask(cur_app)}]" if cur_app else ""
    hint_bot = f" [{_mask(cur_bot)}]" if cur_bot else ""
    hint_owner = f" [{cur_owner}]" if cur_owner else ""

    def _abort_rejected(noun: str) -> None:
        print(
            f"  ⚠️  Slack rejected the {noun} {_SLACK_VERIFY_ATTEMPTS} times — nothing "
            f"was saved. Re-run 'kirocrew setup --slack' once you have a working value.\n"
        )

    # Each value is checked against Slack as it is typed, so a typo, a revoked
    # token, or a channel ID pasted where a member ID belongs is reported HERE —
    # instead of being written as typed and surfacing hours later as a "Slack
    # disabled" line in the gateway log. All of it happens BEFORE the .env lock
    # below is taken: these prompts can block on the user for minutes, and the
    # lock covers only the read-merge-write.
    app_token, _detail = _prompt_verified_slack_value(
        f"  App Token (xapp-...){hint_app}: ",
        cur_app,
        "app token",
        lambda value: _verify_slack_secret(CRED_SLACK_APP_TOKEN, value),
    )
    if app_token is None:
        _abort_rejected("app token")
        return

    bot_token, team = _prompt_verified_slack_value(
        f"  Bot Token (xoxb-...){hint_bot}: ",
        cur_bot,
        "bot token",
        lambda value: _verify_slack_secret(CRED_SLACK_BOT_TOKEN, value),
    )
    if bot_token is None:
        _abort_rejected("bot token")
        return
    if team:
        print(f"  ✅ Bot token verified — workspace: {team}")

    owner_rejected = False
    owner_id, owner_name = _prompt_verified_slack_value(
        f"  Your Slack Member ID{hint_owner}: ",
        cur_owner,
        "member ID",
        lambda value: _verify_slack_owner_id(bot_token or "", value),
    )
    if owner_id is None:
        # NOT an abort. The member ID is optional at write time (see the
        # `if owner_id:` guard below), and the tokens above are already verified —
        # discarding them over an optional field would cost the operator the
        # credentials they just typed, which is the one thing this whole
        # verify-as-you-type flow promises not to do. The full abort stays for a
        # rejected REQUIRED token.
        print(
            f"  ⚠️  Slack rejected the member ID {_SLACK_VERIFY_ATTEMPTS} times — saving "
            "the verified tokens without it. Set it later with "
            "'kirocrew setup --slack'.\n"
        )
        owner_id = ""
        owner_rejected = True
    if owner_name:
        print(f"  ✅ Owner verified: {owner_name} ({owner_id})")

    if not app_token or not bot_token:
        print("  ⚠️  Missing tokens — Slack integration will be disabled.\n")
        return

    # Serialize the read-modify-write against every OTHER .env writer (the
    # `kirocrew secrets import` migrator, the WeChat/Weixin QR handler, and the
    # dashboard channel-credential handlers) on the SAME advisory lock, derived
    # from the shared helper. Without this, a concurrent importer commit (its
    # final CAS + atomic_write) could interleave with this write and clobber the
    # freshly-typed Slack tokens. The prompts above run OUTSIDE the lock (they
    # can block on the user for minutes); the lock wraps only the short
    # re-read → merge → atomic-rename critical section, and we RE-READ .env
    # fresh under the lock so we merge onto the latest on-disk state rather than
    # the possibly-stale snapshot read before the prompts.
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _env_lock_path(cred_path)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
        os.close(lock_fd)
        print(
            "  ⚠️  .env is locked by another process (a secrets import or "
            "credential save is in progress); tokens not saved. Retry once the "
            "other operation finishes.\n",
            file=sys.stderr,
        )
        return
    try:
        # Re-read fresh under the lock, then merge the just-collected tokens on
        # top so a concurrent write that landed during the prompts is preserved.
        merged: dict[str, str] = {}
        if cred_path.exists():
            for line in cred_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    merged[k.strip()] = v.strip()
        merged[CRED_SLACK_APP_TOKEN] = app_token
        merged[CRED_SLACK_BOT_TOKEN] = bot_token
        if owner_id:
            merged[CRED_OWNER_ID] = owner_id
        elif owner_rejected and merged.get(CRED_OWNER_ID):
            # The operator's member ID was rejected, so `merged` still holds the
            # PREVIOUS one, re-read from disk — and the tokens above are brand new.
            # Keeping it blind would pair a stale owner with fresh workspace
            # credentials, which is exactly wrong when the tokens point at a
            # DIFFERENT workspace than the one that owner belongs to.
            #
            # Deleting it blind is not right either: an operator who simply
            # mistyped, on the same workspace, would silently lose a good owner ID.
            # So the stale value is re-checked against the NEW bot token and only
            # dropped when Slack INDICTS it (wrong workspace / not a member ID).
            # `None` means unverifiable (a network or scope error), where deleting
            # would be data loss on a transient failure, so it is kept.
            stale_owner = merged[CRED_OWNER_ID]
            stale_ok, stale_detail = _verify_slack_owner_id(bot_token or "", stale_owner)
            if stale_ok is False:
                del merged[CRED_OWNER_ID]
                print(
                    f"  ⚠️  Also dropped the saved member ID {stale_owner} — it is not "
                    f"valid for these tokens ({stale_detail}). Set it with "
                    "'kirocrew setup --slack'.\n"
                )
            else:
                print(f"  ℹ️  Keeping the saved member ID {stale_owner}.\n")
        lines = [f"{k}={v}" for k, v in merged.items()]
        # atomic_write with restrict_to_owner, not write_text + chmod(0o600): a
        # bare chmod is a silent no-op for Windows ACLs, and applying any lockdown
        # only AFTER the tokens are on disk leaves them readable through the
        # directory's inherited DACL in the failure window. The helper locks its
        # unique temp file down before any content reaches it and renames only on
        # success, so the tokens never exist under a wider mode and a failure
        # leaves the previous .env untouched. restrict_on_error="warn" matches the
        # .env doctrine (enforce the lockdown, log a warning if it fails) and the
        # dashboard credential writers: an ACL-refusing host still completes the
        # wizard instead of aborting after the user typed their tokens.
        atomic_write(
            cred_path,
            "\n".join(lines) + "\n",
            restrict_to_owner=True,
            restrict_on_error="warn",
        )
    finally:
        platform_compat.release_lock(lock_fd)
        os.close(lock_fd)
    print(f"  ✅ Credentials saved to {cred_path}\n")
    # Slack credentials are read once, at gateway startup, so tokens written by
    # this step stay inert until it restarts -- and the bug the operator then
    # reports is "I fixed the token and Slack is still silent". Naming the command
    # removes that surprise; OFFERING to run it was ~80 lines and 8 tests to save
    # one command, and it could drop every in-flight session or spawn a gateway
    # nobody asked this wizard to start.
    print("  Restart the gateway to pick them up: kirocrew restart\n")


def _setup_whatsapp() -> None:
    """Guided WhatsApp opt-in: report the prerequisites, then enable the channel.

    There is no token to collect: WhatsApp pairs as a linked device on the
    operator's own account, and pairing is a QR scan served by the RUNNING gateway.
    So this step's whole job is the three things an operator cannot discover from
    anywhere else: that automating a personal account carries a ban risk, whether
    the optional wheel the channel needs is installed, and that enabling the
    channel is a config flag separate from pairing it.

    ``neonize_available()`` is a ``find_spec`` check, so nothing here imports
    neonize or opens the session store: the wizard reports on the credential's
    path, never through it.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.whatsapp.client import (
        MISSING_EXTRA_HINT,
        default_db_path,
        neonize_available,
    )

    print("── WhatsApp ──\n")
    print("  WhatsApp links as a device on your OWN account. There is no bot")
    print("  identity, so the agent sends as you.")
    print("  Automating a personal account is against WhatsApp's Terms of Service")
    print("  and carries a small risk of the linked number being banned. Keep")
    print("  volumes personal-scale.\n")

    if neonize_available():
        print("  ✅ The 'whatsapp' dependency extra is installed")
    else:
        print("  ⚠️  The 'whatsapp' extra is NOT installed, so the channel cannot start")
        print(f"     {MISSING_EXTRA_HINT}")

    store = default_db_path(data_home())
    if store.exists():
        print(f"  ✅ A paired session store already exists: {store}")
    else:
        print("  ℹ️  Not paired yet. Pairing is a QR scan from the dashboard:")
        print("     Settings → Channels → WhatsApp, with the gateway running.")
    print()

    answer = _input_or_skip("  Enable the WhatsApp channel? [y/N]: ")
    if not answer or answer.lower() not in ("y", "yes"):
        print("  ⏭  Left disabled. Enable it later from Settings → Channels.\n")
        return

    cfg_file = config_path()
    cfg: dict = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}\n")
            return
        # A top-level non-object is not something this step may repair: writing our
        # own object over it would destroy whatever the operator meant. Mirrors
        # _setup_sandbox_consent.
        if not isinstance(loaded, dict):
            print(f"  ⚠️  {cfg_file} does not contain a JSON object; skipping.\n")
            return
        cfg = loaded
    if not isinstance(cfg.get("whatsapp"), dict) and "whatsapp" in cfg:
        print("  ⚠️  'whatsapp' section is not an object; leaving config untouched.\n")
        return

    # The read above answered "may this step run"; it is NOT the read the write
    # is derived from. That one happens inside ``update_config_locked``'s hold on
    # the ``<config>.lock`` sidecar, so a dashboard settings write or a
    # ``kirocrew config set`` landing between the two is carried forward instead
    # of being replaced by this older snapshot.
    section_clash: list[str] = []

    def _enable(data: dict) -> dict | None:
        section = data.get("whatsapp")
        if not isinstance(section, dict):
            if "whatsapp" in data:
                # Re-checked under the lock: another writer may have replaced the
                # section since the read above. Skip the write, report outside.
                section_clash.append("whatsapp")
                return None
            section = {}
            data["whatsapp"] = section
        section["enabled"] = True
        return data

    try:
        update_config_locked(cfg_file, mutate=_enable, stamp_meta=False)
    except ConfigReadError as exc:
        # Does not inherit OSError, so it needs naming next to the write failure.
        print(f"  ⚠️  Could not read {cfg_file}: {exc}\n")
        return
    except OSError as exc:
        print(f"  ⚠️  Could not write {cfg_file}: {exc}")
        print("     Nothing was enabled. Enable it from Settings → Channels instead.\n")
        return
    if section_clash:
        print("  ⚠️  'whatsapp' section is not an object; leaving config untouched.\n")
        return
    print("  ✅ Recorded: whatsapp.enabled = true")
    print("     Next: start the gateway, then scan the QR from")
    print("     Settings → Channels → WhatsApp.\n")


_CUSTOM_DOMAIN = "kirocrew.localhost"


def _configured_port() -> int:
    """Return the dashboard port using the same resolution as other CLI commands."""
    from kiro_crew.cli_server import (
        resolve_client_port,  # local import keeps cli_server (heavy import graph) out of cli_setup's import time
    )

    return resolve_client_port(None)


def _detect_system_timezone() -> str:
    """Return the system IANA tz name, or empty string if it can't be detected.

    ``TZ`` and ``/etc/localtime`` are POSIX-only. On Windows the zone lives in
    the registry as a Windows zone id ("Pacific Standard Time") that needs a
    Windows->IANA mapping, so without a Windows branch this returned "" there and
    the whole product silently fell back to UTC — cron schedules and the agent's
    "today" were off by the local offset. ``tzlocal`` (a Windows-marked dep whose
    only requirement, ``tzdata``, we already ship) resolves it via the CLDR
    windowsZones table; it is imported lazily so a source checkout without it
    degrades to the old skip-and-ask flow rather than crashing.
    """
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env and not tz_env.startswith("/"):
        return tz_env
    try:
        p = Path("/etc/localtime")
        if p.is_symlink():
            target = str(p.resolve())
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    if platform_compat.IS_WINDOWS:
        try:
            # tzlocal ships no type stubs; it is a Windows-only lazy import, so
            # ignore the missing-stub error rather than add a types-* dep.
            import tzlocal  # type: ignore[import-untyped]

            name = tzlocal.get_localzone_name()
            if name:
                return str(name)
        except Exception:
            # No tzlocal / unmappable zone: fall through to the ask-or-skip path
            # rather than crash the setup wizard.
            pass
    return ""


class _SetupAborted(Exception):
    """Raised when stdin is closed mid-wizard.

    The wizard is a sequential chain of interactive prompts; if stdin closes at
    ANY prompt, every subsequent bare ``input()`` would traceback. Callers use
    ``_input_or_skip`` for guarded prompts (workspace, slash-command, timezone);
    the top-level ``_setup`` catches this and exits cleanly, so a
    ``kirocrew setup < /dev/null`` — or a Windows console quirk that closes
    stdin — surfaces one clean message instead of a stack trace.
    """


def _input_or_skip(prompt: str) -> str | None:
    """``input(prompt).strip()``, or raise ``_SetupAborted`` on EOF.

    Returns ``None`` when the user hit Enter with no input, which callers treat
    as "keep the default / skip this step". A closed/piped stdin is a different
    condition and must not be silently coerced to ``""`` (coercing it admits an
    empty default and cascades the failure into the NEXT step's bare
    ``input()``) — see ``_SetupAborted``. A non-UTF-8 locale (e.g. C/POSIX)
    makes ``input()`` raise ``UnicodeDecodeError`` the same way, so it is
    treated identically.
    """

    try:
        answer = input(prompt).strip()
    except (EOFError, UnicodeDecodeError) as exc:
        raise _SetupAborted("stdin closed; setup cannot continue") from exc
    return answer or None


def _setup_slash_command() -> None:
    """Prompt for custom slash command name, save to config.json."""
    cfg_file = config_path()
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return

    print("── Slash Command ──\n")
    # A non-object ``slack`` is an operator value this step cannot merge into.
    # Guarded HERE as well as in the write below, because this read is what runs
    # first: ``.get("slack", {}).get(...)`` raised AttributeError on a scalar and
    # took the whole wizard down with a traceback. Refusing the step is the same
    # answer the whatsapp and sandbox steps give for their own sections.
    slack_section = cfg.get("slack")
    if slack_section is not None and not isinstance(slack_section, dict):
        print("  ⚠️  'slack' section is not an object; leaving config untouched.\n")
        return
    current = (slack_section or {}).get("command", "kirocrew")
    # EOF keeps the current value (same reasoning as the workspace step).
    raw = _input_or_skip(f"  Slash command name [{current}]: ") or ""
    if raw:
        raw = raw.lstrip("/").strip()
    if not raw:
        raw = current
    if not all(c.isalnum() or c in "-_" for c in raw):
        print("  ⚠️  Command name should only contain letters, numbers, hyphens, or underscores.")
        raw = current
    if len(raw) > 32:
        print("  ⚠️  Command name too long (max 32 chars).")
        raw = current

    # Re-checked under the lock, and it must ABORT rather than replace: a
    # non-dict ``slack`` is an operator value this step did not write and cannot
    # merge into, so overwriting it with a fresh object would destroy it while
    # reporting success. Absent is the only case that may be created. Same rule
    # as the whatsapp and sandbox steps above.
    section_clash: list[str] = []

    def _apply(data: dict) -> dict | None:
        section = data.get("slack")
        if not isinstance(section, dict):
            if "slack" in data:
                section_clash.append("slack")
                return None
            section = {}
            data["slack"] = section
        section["command"] = raw
        return data

    try:
        update_config_locked(cfg_file, mutate=_apply, stamp_meta=False)
    except ConfigReadError as exc:
        print(f"  ⚠️  Could not read {cfg_file}: {exc}")
        return
    if section_clash:
        print("  ⚠️  'slack' section is not an object; leaving config untouched.\n")
        return
    print(f"  ✅ Slash command: /{raw}\n")


def _setup_sandbox_consent() -> None:
    """Surface the unconfined-exec decision when this host has NO sandbox backend.

    Which WAY this step asks depends on what an undeclared key resolves to here,
    via :func:`~kiro_crew.sandbox.unsandboxed_exec_platform_default`.

    On a platform where a backend is BROKEN or missing but installable — a Linux
    kernel refusing user namespaces, a macOS host — fail-closed remains the
    shipped posture and this step offers the opt-IN, writing the key only on an
    explicit yes. The reasoning is unchanged and the threat is concrete: an
    agent-selected repo's ``include.path`` reaches ``~/.aws/credentials``, and a
    crafted ``.tex`` typesets a secret into a PDF. Such a host also has a remedy
    that RESTORES isolation, so pointing at the profile beats disabling the check.

    On a platform with no backend to install at all — Windows, where there is
    neither a user namespace nor ``sandbox-exec`` — the effective default is now
    ALLOW, and this step offers the opt-OUT instead. This is a deliberate product
    decision with a real cost, recorded here rather than glossed: it removes a
    deny-by-default authorization, and the attack above is exactly as available
    afterwards as it was before. It was taken because fail-closed there is not a
    security posture anyone can act on — no operator action produces a backend, so
    the check refused every MCP server, app backend and provider CLI on the
    platform in perpetuity, and the practical outcome was users hand-editing the
    same flag from an error message with no risk statement attached at all.

    What stands in the deny-by-default's place, none of it equivalent to it:

    * this step, which STATES the exposure in the same words the opt-in used and
      offers the lockdown, including a printed notice on a non-interactive run;
    * a per-spawn SEL ``outcome="unconfined"`` event naming whether an operator or
      the platform permitted it, so an unconfined spawn is never unrecorded;
    * the loud once-per-process ``SECURITY`` log line from ``_warn_no_isolation``;
    * a governance ``sandbox.min_level`` floor, which still OVERRIDES the default
      fleet-wide and keeps a managed host fail-closed;
    * a declared ``false``, which still outranks the platform default — an
      operator who locked this host down stays locked down.

    Only a genuine ``"no_backend"`` classification reaches either prompt.
    ``detect_backend() == "none"`` alone is not sufficient: it also covers a
    momentary fork/resource failure, which self-heals on the next spawn and must
    never buy a permanent bypass, and a foreign outer sandbox, where this host's
    sandbox works and the remedy hands isolation back to Kiro Crew rather than
    disabling it. Neither prompt is shown when stdin/stdout are not both a
    terminal, because an unseen question is a hang rather than consent — but on a
    default-allow platform the notice is printed there anyway, since that run is
    otherwise told nothing.

    Silent no-op when a backend exists (the Linux/macOS norm) or when the key is
    already declared in either state, in ``config.json`` OR the
    ``config.local.json`` overlay that deep-merges over it — the overlay wins at
    load time, so ignoring it would let this step prompt a user who already
    decided and then report a state the effective config contradicts. Declining —
    including a non-interactive EOF, which :func:`_input_or_skip` reports as
    ``None`` — leaves the config untouched in BOTH directions, so the host keeps
    this platform's default AND stays undeclared, and a later change to that
    default still reaches it.
    """
    try:
        kind = unavailable_kind()
    except Exception as exc:  # pragma: no cover — defensive
        print(f"  ⚠️  Could not probe for a sandbox backend: {exc}")
        return
    if kind == "transient":
        # A momentary fork/resource failure is NOT grounds for a permanent
        # bypass: it is not cached, the next spawn re-probes, and the sandbox
        # layer's own guidance says explicitly not to disable the sandbox for it.
        print("── Sandbox ──\n")
        print("  The sandbox probe failed for what looks like a TRANSIENT reason")
        print("  (momentary resource pressure). It re-probes automatically, so no")
        print("  opt-in is offered. Retry the operation instead.\n")
        return
    if kind != "no_backend":
        # "" (a backend exists — the Linux/macOS norm) or "foreign_sandbox",
        # where this host's sandbox works and the remedy hands isolation back to
        # Kiro Crew rather than disabling it. Neither warrants this opt-in.
        return
    # Which way this step ASKS depends on what an undeclared key now resolves to
    # on this platform. Resolved once, up front, because the non-interactive
    # notice, the question and the value written must all describe the same
    # default -- and on a platform whose default is "allowed" the old wording
    # ("subprocesses are refused") would be simply false.
    unconfined_by_default = unsandboxed_exec_platform_default()
    # Nothing to surface once the operator DECLARED the key, in either state, in
    # config.json OR the config.local.json overlay that deep-merges over it — the
    # overlay wins at load time, so ignoring it would let this step address a user
    # who already decided. This check comes BEFORE the non-TTY notice on purpose:
    # that notice describes the PLATFORM DEFAULT, so printing it to a host that
    # declared the opposite would tell an operator who locked the host down that it
    # runs unconfined.
    if unsandboxed_exec_declared():
        return
    # A prompt nobody can see is a hang, not consent: `kirocrew update` runs
    # setup with its output captured and stdin on DEVNULL, so a question asked
    # there is invisible and reads EOF. This guard keeps the decision at a real
    # terminal rather than letting a non-interactive run answer it.
    if not _stdio_is_interactive():
        if unconfined_by_default:
            # Still printed, not skipped: this is the only notice a non-interactive
            # install gets that its agent subprocesses run unconfined, and the
            # whole point of a platform default is that nobody had to consent to
            # it. Saying nothing here is what would make the change silent.
            print("  ⚠️  This platform offers no OS-level sandbox backend, so agent")
            print("     subprocesses run WITHOUT credential isolation by default.")
            print("     Run `kirocrew setup` from a terminal to review that, or set")
            print("     agent.sandbox_allow_unsandboxed_exec=false by hand to refuse")
            print("     them instead.\n")
        else:
            print("  ⚠️  No sandbox backend on this host, so agent subprocesses are")
            print("     refused. Run `kirocrew setup` from a terminal to decide, or set")
            print("     agent.sandbox_allow_unsandboxed_exec=true by hand to opt in.\n")
        return

    cfg_file = config_path()
    cfg: dict = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return
        # A top-level non-object (`[]`, `"x"`, `null`) is not something this step
        # may repair: writing our own object over it would destroy whatever the
        # operator meant, and `.get()` on it raises. Report and leave it alone —
        # the effective default stays fail-closed either way.
        if not isinstance(loaded, dict):
            print(f"  ⚠️  {cfg_file} does not contain a JSON object; skipping.\n")
            return
        cfg = loaded

    print("── Sandbox ──\n")
    if unconfined_by_default:
        # INFORM, then offer the restriction. The risk paragraph is identical to
        # the opt-in branch's on purpose: what changed is who has to act, not how
        # dangerous it is, and a default that describes itself in softer words
        # than the opt-in did would be the silent change this branch exists to
        # avoid.
        print("  This platform offers no OS-level sandbox backend for Kiro Crew to")
        print("  apply — there is no Linux user namespace and no macOS sandbox-exec,")
        print("  and unlike a misconfigured Linux host there is no profile to")
        print("  install that would produce one.")
        print()
        print("  Agent subprocesses therefore run unconfined here BY DEFAULT: an")
        print("  agent-driven subprocess can read your home directory, including")
        print("  ~/.aws and ~/.ssh, with no OS confinement. Kiro Crew still scrubs")
        print("  credential environment variables and audits every such spawn, but")
        print("  it cannot stop a hostile repo or document from reading files.")
        print()
        print("  Refusing them instead disables MCP servers, Dev Fleet and the")
        print("  Papyrus compiler on this host — they will report a sandbox error.")
        print()
        answer = _input_or_skip("  Refuse unsandboxed execution on this host? [y/N]: ")
        granting = False
    else:
        print("  This host offers no OS-level sandbox backend (Linux user namespaces")
        print("  or macOS sandbox-exec), so Kiro Crew currently REFUSES to run agent")
        print("  subprocesses at all — MCP servers, Dev Fleet and the Papyrus")
        print("  compiler will report a sandbox error until you decide.")
        print()
        print("  Allowing them to run unconfined means an agent-driven subprocess can")
        print("  read your home directory, including ~/.aws and ~/.ssh, with no OS")
        print("  confinement. Kiro Crew still scrubs credential environment variables,")
        print("  but it cannot stop a hostile repo or document from reading files.")
        print()
        answer = _input_or_skip("  Allow unsandboxed execution? [y/N]: ")
        granting = True
    if not answer or answer.lower() not in ("y", "yes"):
        # Declining writes nothing in BOTH directions, so the effective state
        # stays whatever this platform's default is -- and stays undeclared, so a
        # later change to that default still reaches this host. Writing the
        # current default on a decline would freeze it silently.
        if unconfined_by_default:
            print("  ⏭  Left as-is — agent subprocesses keep running unconfined here.")
            print("     To refuse them later, set")
            print("     agent.sandbox_allow_unsandboxed_exec=false")
            print(f"     in {cfg_file}\n")
        else:
            print("  ⏭  Left fail-closed — MCP tooling stays disabled on this host.")
            print("     To opt in later, set agent.sandbox_allow_unsandboxed_exec=true")
            print(f"     in {cfg_file}\n")
        return

    if not isinstance(cfg.get("agent"), dict) and "agent" in cfg:
        print("  ⚠️  'agent' section is not an object; leaving config untouched.\n")
        return

    # Audit BEFORE the write: this persists an execution permission, so it belongs
    # in the tamper-evident log next to the ``denied`` event ``wrap_argv`` emits
    # when it refuses a spawn — otherwise the refusals are recorded and the grant
    # that silences them is not. Audit-then-write is the safe ordering: a failure
    # between the two leaves a record without a grant, never a grant without a
    # record. The documented manual ``config.json`` edit remains available; it is
    # outside this wizard's control and is not a bypass this step introduces.
    #
    # ``critical`` — and whether an audit failure ABORTS — depends on the
    # direction, because the two directions fail unsafe in opposite ways. A GRANT
    # is audit-or-deny: ``critical=True`` makes SEL write synchronously and
    # re-raise, and the grant is refused rather than persisted unaudited. A
    # RESTRICTION is best-effort: refusing to record "keep this host fail-closed"
    # because the audit log is broken would LEAVE THE HOST UNCONFINED, which is
    # the more dangerous of the two outcomes, so the write proceeds and the audit
    # gap is reported instead.
    try:
        sel().log_tool_invocation(
            session_key="setup",
            agent="system",
            source="cli_setup._setup_sandbox_consent",
            tool_name="sandbox_allow_unsandboxed_exec",
            tool_kind="config",
            outcome="allowed" if granting else "denied",
            resources=str(cfg_file),
            metadata={
                "reason": (
                    "operator_consent_at_setup" if granting else "operator_lockdown_at_setup"
                ),
                "probe_kind": kind,
            },
            critical=granting,
        )
    except Exception as exc:
        print(f"  ⚠️  Could not record the security audit event: {exc}")
        if granting:
            print("     Refusing to grant unsandboxed execution unaudited —")
            print("     left fail-closed. Fix the audit log, then re-run setup.\n")
            return
        print("     Recording the restriction anyway: refusing to fail-close this")
        print("     host because the audit log is broken would leave it unconfined.\n")

    # Under the sidecar lock, and the grant is applied to the document as it
    # stands there -- the snapshot read before the prompt is only what decided
    # whether to ask. The audit above stays ahead of the acquire, so the
    # audit-then-write ordering is unchanged.
    section_clash: list[str] = []

    def _grant(data: dict) -> dict | None:
        section = data.get("agent")
        if not isinstance(section, dict):
            if "agent" in data:
                section_clash.append("agent")
                return None
            section = {}
            data["agent"] = section
        section["sandbox_allow_unsandboxed_exec"] = granting
        return data

    # A failed write left the key UNDECLARED, so the host keeps this platform's
    # default -- which is not "fail-closed" everywhere. Naming the wrong outcome
    # here would tell an operator their host is protected when nothing confines
    # it, so both messages state the default that actually now applies.
    unchanged = (
        "agent subprocesses keep running unconfined here"
        if unconfined_by_default
        else "the host stays fail-closed"
    )
    byhand = "false" if unconfined_by_default else "true"
    try:
        update_config_locked(cfg_file, mutate=_grant, stamp_meta=False)
    except ConfigReadError as exc:
        print(f"  ⚠️  Could not read {cfg_file}: {exc}")
        print(f"     Nothing was recorded — {unchanged}.\n")
        return
    except OSError as exc:
        # A locked or read-only config (common on Windows when another process
        # holds it) must not abort the whole wizard after the user has already
        # answered. Report it and continue: nothing was written, so the host keeps
        # whatever this platform's default is.
        print(f"  ⚠️  Could not write {cfg_file}: {exc}")
        print(f"     Nothing was recorded — {unchanged}. Set")
        print(f"     agent.sandbox_allow_unsandboxed_exec={byhand} by hand instead.\n")
        return
    if section_clash:
        print("  ⚠️  'agent' section is not an object; leaving config untouched.\n")
        return
    print(f"  ✅ Recorded: agent.sandbox_allow_unsandboxed_exec = {str(granting).lower()}\n")


def _setup_timezone() -> None:
    """Auto-detect timezone and save to config.json."""
    cfg_file = config_path()

    # Check if already configured
    data: dict = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return
    current = data.get("timezone", "")

    # Auto-detect from system
    detected = _detect_system_timezone()

    print("── Timezone ──\n")
    if current:
        print(f"  Current: {current}")
        answer = _input_or_skip(f"  Timezone [{current}]: ")
        if not answer:
            print(f"  ✅ Keeping: {current}\n")
            return
        tz_val = answer
    elif detected:
        print(f"  Detected: {detected}")
        answer = _input_or_skip(f"  Timezone [{detected}]: ")
        tz_val = answer or detected
    else:
        raw = _input_or_skip("  IANA timezone (e.g. America/Los_Angeles): ")
        if not raw:
            print("  ⏭  Skipped. Cron schedules will show UTC.\n")
            return
        tz_val = raw

    # Validate with retry
    abbrev_to_iana: dict[str, str] = {
        "PST": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "MST": "America/Denver",
        "MDT": "America/Denver",
        "CST": "America/Chicago",
        "CDT": "America/Chicago",
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "GMT": "Etc/GMT",
        "BST": "Europe/London",
        "CET": "Europe/Berlin",
        "CEST": "Europe/Berlin",
        "IST": "Asia/Kolkata",
        "JST": "Asia/Tokyo",
        "AEST": "Australia/Sydney",
        "AEDT": "Australia/Sydney",
        "NZST": "Pacific/Auckland",
        "NZDT": "Pacific/Auckland",
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            ZoneInfo(tz_val)
            break  # valid
        except (KeyError, Exception):
            suggestion = abbrev_to_iana.get(tz_val.upper())
            if suggestion:
                print(f"  ❌ '{tz_val}' is an abbreviation, not an IANA timezone.")
                print(f"     Did you mean: {suggestion}?")
            else:
                print(f"  ❌ Unknown timezone '{tz_val}'.")
                print("     Use IANA format, e.g. America/Los_Angeles, Europe/London")
            if attempt < max_retries - 1:
                retry = _input_or_skip("  Timezone: ")
                if not retry:
                    print("  ⏭  Skipped.\n")
                    return
                tz_val = retry
            else:
                print("  ⏭  Skipped after too many attempts.\n")
                return

    def _apply(existing: dict) -> dict:
        existing["timezone"] = tz_val
        return existing

    try:
        update_config_locked(cfg_file, mutate=_apply, stamp_meta=False)
    except ConfigReadError as exc:
        # The pre-prompt read already refuses a corrupt config; this covers a
        # file that went bad while the operator was answering, and refuses the
        # same way rather than surfacing a traceback out of the wizard.
        print(f"  ⚠️  Could not read {cfg_file}: {exc}")
        return
    print(f"  ✅ Timezone saved: {tz_val}\n")


def _maybe_setup_dashboard_url() -> None:
    """Prompt for dashboard.url when running on a remote host with Slack configured."""

    cfg_file = config_path()
    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))

    if not has_slack:
        return  # No Slack → local-only, no URL needed

    # Detect if this looks like a remote host
    try:
        ip = socket.gethostbyname(socket.gethostname())
        is_remote = not ip.startswith("127.")
    except OSError:
        is_remote = False

    if not is_remote and not cfg.dashboard.url:
        return  # Localhost machine with no existing URL config — skip

    current = cfg.dashboard.url
    hostname = socket.gethostname()

    print("── Dashboard URL (remote access) ──\n")
    if is_remote:
        print(f"  This host ({hostname}) appears to be a remote machine.")
        print("  Setting a dashboard URL enables direct browser access with token auth.")
        print("  Leave blank for localhost-only (SSH tunnel required).\n")
    else:
        print("  Configure a custom dashboard URL for remote access.")
        print("  Leave blank for localhost-only.\n")

    hint = f" [{current}]" if current else ""
    answer = input(f"  Dashboard URL (e.g. http://{hostname}:{_configured_port()}){hint}: ").strip()

    if answer == "" and current:
        print(f"  ✅ Keeping: {current}\n")
        return
    if answer == "" and not current:
        print("  ⏭  Skipped. Dashboard will bind to localhost only.\n")
        return

    # Persist to config.json. The read is inside the lock hold, so the URL
    # cannot be written over a document that predates another writer's change.
    #
    # A non-dict ``dashboard`` RAISES rather than being replaced: it is an
    # operator value this step cannot merge into, and the broad handler below
    # already reports exactly that as "Failed to save" and writes nothing --
    # which is what this step did before it took the lock, when the same shape
    # raised out of ``setdefault``.
    def _apply(data: dict) -> dict:
        dashboard = data.get("dashboard")
        if dashboard is None and "dashboard" not in data:
            dashboard = {}
            data["dashboard"] = dashboard
        elif not isinstance(dashboard, dict):
            raise TypeError(f"'dashboard' in {cfg_file} is not an object; refusing to replace it")
        dashboard["url"] = answer
        return data

    try:
        update_config_locked(cfg_file, mutate=_apply, stamp_meta=False)
        print(f"  ✅ Dashboard URL saved: {answer}")
        print("  Token auth will be required for all requests.\n")
    except Exception as e:
        print(f"  ❌ Failed to save: {e}\n")


def _maybe_setup_custom_domain() -> None:
    """Inform user of the dashboard URL and clean up legacy mesh.claw from /etc/hosts."""
    port = _configured_port()
    print("\n── Dashboard URL ──\n")
    print(f"  Dashboard available at http://localhost:{port}")
    print(f"  Optional friendlier alias: http://{_CUSTOM_DOMAIN}:{port}")
    print(
        "  (the alias needs no /etc/hosts edit only on browsers that honor RFC 6761\n"
        "   *.localhost — Chrome/Firefox/Linux. Safari and the macOS resolver do NOT\n"
        "   map *.localhost, so prefer plain localhost, especially when tunneling in.)\n"
    )

    # Advise removal of legacy "mesh.claw" entry from /etc/hosts if present
    try:
        if "mesh.claw" in Path("/etc/hosts").read_text(encoding="utf-8"):
            print("  ⚠  Legacy mesh.claw entry found in /etc/hosts.")
            print(
                "  To remove it: sudo grep -v 'mesh\\.claw' /etc/hosts > /tmp/hosts.clean"
                " && sudo mv /tmp/hosts.clean /etc/hosts\n"
            )
    except Exception:
        pass
