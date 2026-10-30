"""The Slack App Home tab: ``_publish_home_tab`` builds and publishes its Block Kit view.

The view carries a status header, the MCP servers and skills (listed through
``_get_skills_loader``, a listing-only loader created on first use), the cron jobs, the
recent sessions (collected off the event loop, one collection at a time through
``_home_tab_collect_gate``), the recent lessons and the registered slash commands.

Split out of ``kiro_crew.slack.events``, which stays the import path and the patch
target: every name here is reached as ``kiro_crew.slack.events.<name>``, and a patch
through that facade reaches this module (``kiro_crew._facade_mirror``). Nothing here
imports the facade. New Home tab work goes here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from kiro_crew import __version__
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import format_schedule
from kiro_crew.dashboard.handlers import get_update_info
from kiro_crew.mcp_discovery import list_servers
from kiro_crew.platform import current_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.blocks import command_hint_block
from kiro_crew.slack.events_slash import SLASH_REGISTRY
from kiro_crew.slack.handler import is_allowed_user, is_owner, is_yolo_mode, slack_cfg
from kiro_crew.slack.sessions_view import (
    _HOME_TAB_SESSIONS_PER_KIND,
    _SESSION_KIND_DASHBOARD,
    _SESSION_KIND_TASKRUNNER,
    _build_sessions_blocks,
    _collect_recent_sessions_off_loop,
)
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.events")


_skills_loader: SkillsLoader | None = None


def _get_skills_loader() -> SkillsLoader:
    global _skills_loader  # noqa: PLW0603
    if _skills_loader is None:
        # Listing only: the gateway startup already ran the builtin sync (in a
        # worker thread). Re-syncing here would put its filesystem work on the
        # event loop that renders the Slack Home tab.
        _skills_loader = SkillsLoader(install_builtins=False)
    return _skills_loader


#: How many Home Tab session collections may run at once. Every
#: ``app_home_opened`` from an allowed user schedules its own publish, and the
#: collector reads up to ``per_kind * 10`` transcripts on worker threads. Those
#: threads are the process-wide default executor, shared with history appends,
#: cron store writes and session storage, so an unbounded fan-out of tab opens
#: makes unrelated ``asyncio.to_thread`` callers queue behind multi-MB reads.
#: One at a time is what the collector had before it was offloaded, when running
#: on the loop serialized it by accident; this keeps that bound on purpose.
_HOME_TAB_COLLECT_CONCURRENCY = 1
_home_tab_collect_sem: asyncio.Semaphore | None = None


def _home_tab_collect_gate() -> asyncio.Semaphore:
    """The collector gate, created on the loop that first needs it.

    Built lazily rather than at import: a module-level ``asyncio.Semaphore``
    binds to whatever loop happens to be current when the module loads, and the
    gateway's loop is not running yet at that point.
    """
    global _home_tab_collect_sem
    if _home_tab_collect_sem is None:
        _home_tab_collect_sem = asyncio.Semaphore(_HOME_TAB_COLLECT_CONCURRENCY)
    return _home_tab_collect_sem


# ---------------------------------------------------------------------------
# Home Tab
# ---------------------------------------------------------------------------


async def _publish_home_tab(orch: GatewayOrchestrator, user_id: str) -> None:
    """Build and publish the Block Kit Home Tab view."""
    try:
        blocks: list[dict] = []

        # ── Status ──
        yolo = is_yolo_mode()
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": "👻 Kiro Crew Status"}}
        )
        status_lines = [
            "*Gateway:* ✅ Online",
            f"*YOLO mode:* {'🟢 ON' if yolo else '🔴 OFF'}",
        ]
        if orch.sessions is not None:
            status_lines.append(f"*Active sessions:* {orch.sessions.count}")
        status_lines.append(f"*Uptime:* {Stats().uptime_str()}")
        status_lines.append(await current_context().identity.status_line())
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(status_lines)}}
        )
        blocks.append({"type": "divider"})

        # ── Capabilities ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🔌 Capabilities"}})
        try:
            # Only servers a session can actually use: ``disabled`` is the
            # aggregate of the launch predicate over every scope, so a server
            # switched off in the shared config -- or muted by a non-boolean
            # ``disabled`` -- is not advertised as a capability here.
            servers = [s for s in await asyncio.to_thread(list_servers) if not s.disabled]
            skills = await asyncio.to_thread(lambda: _get_skills_loader().list_skills())

            # Slack caps a single section's text at 3000 chars. MCP servers and
            # skills each get their OWN section with an independent length cap
            # (appending "…and N more" when the list won't fit) — an uncapped
            # list (e.g. 100+ skills) would overflow and make views.publish fail
            # with invalid_arguments, breaking the whole Home tab. Mirrors the
            # cron block's jobs[:15] guard below.
            def _capped_names_section(label: str, names: list[str], budget: int = 2900) -> dict:
                total = len(names)
                prefix = f"*{label} ({total}):* "
                suffix_room = 24  # reserve for "  _…and N more_"
                shown: list[str] = []
                used = len(prefix)
                for nm in names:
                    add = (", " if shown else "") + nm
                    if used + len(add) > budget - suffix_room:
                        break
                    shown.append(nm)
                    used += len(add)
                line = prefix + ", ".join(shown)
                if len(shown) < total:
                    line += f"  _…and {total - len(shown)} more_"
                # Defense-in-depth redaction ("never trust output").
                line = redact_credentials(redact_exfiltration_urls(line)[0])[0]
                return {"type": "section", "text": {"type": "mrkdwn", "text": line}}

            if servers:
                blocks.append(_capped_names_section("MCP Integrations", [s.name for s in servers]))
            if skills:
                blocks.append(_capped_names_section("Skills", [s["name"] for s in skills]))
            if not servers and not skills:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "_No MCP servers or skills configured._",
                        },
                    }
                )
        except Exception:
            logger.error("Failed to load capabilities for home tab", exc_info=True)
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Capabilities unavailable._"},
                }
            )
        blocks.append({"type": "divider"})

        # ── Cron Jobs ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "⏰ Cron Jobs"}})
        if orch.cron_svc is not None:
            jobs = orch.cron_svc.list_jobs(include_disabled=True)
            if jobs:
                try:
                    _tz = KiroCrewConfig.load().timezone
                except Exception:
                    _tz = ""
                if not _tz and orch.slack is not None:
                    try:
                        profile = await orch.slack.get_user_profile(user_id)
                        _tz = profile.get("timezone", "")
                    except Exception:
                        _tz = ""
                lines = []
                for j in jobs[:15]:
                    status = "✅" if j.enabled else "⏸️"
                    # Each job carries its own timezone; honour it first and
                    # fall back to the configured/viewer zone only when the job
                    # has none, matching the scheduler and the other cron
                    # display surfaces.
                    sched = format_schedule(j.schedule, tz_name=getattr(j, "timezone", "") or _tz)
                    raw = f"{status} *{j.name}* — `{sched}`"
                    lines.append(redact_credentials(redact_exfiltration_urls(raw)[0])[0])
                if len(jobs) > 15:
                    lines.append(f"_…and {len(jobs) - 15} more_")
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
                )
            else:
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "_No cron jobs._"}}
                )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Cron service unavailable._"},
                }
            )
        blocks.append({"type": "divider"})

        # ── Sessions (main chat + task runner) ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🧵 Sessions"}})
        # Deny-by-default authorization gate (defense-in-depth).
        #
        # Session JSONLs contain prior conversation contents. The dispatcher
        # already filters ``app_home_opened`` events via ``is_allowed_user``
        # at events.py before calling _publish_home_tab, so in production
        # this branch is unreachable today. The check is still required by
        # the security-controls rule (deny-by-default) and protects
        # against future refactors that bypass the dispatcher gate. Mirrors
        # the slash-command pattern at events._handle_sessions.
        if not (is_owner(user_id) or is_allowed_user(user_id)):
            sel().log_api_access(
                caller=user_id,
                operation="slack.home_tab_sessions_data_access",
                outcome="denied",
                source="slack",
                resources="home_tab",
                error="unauthorized caller",
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Sessions unavailable._"},
                }
            )
            blocks.append({"type": "divider"})
            # Skip ahead to the next section (lessons), bypassing the
            # try block below which would otherwise read sessions.
        else:
            try:
                sess_mgr = orch.sessions
                # Read per-kind cap from config (default 5).
                try:
                    per_kind = slack_cfg(orch).slack.home_tab_sessions_per_kind
                    if not isinstance(per_kind, int) or per_kind < 1:
                        per_kind = _HOME_TAB_SESSIONS_PER_KIND
                except (AttributeError, TypeError):
                    per_kind = _HOME_TAB_SESSIONS_PER_KIND
                # Single directory scan for both kinds; partition + cap in memory.
                # Gated so a burst of tab opens cannot put N of these scans in
                # the shared executor at once; the rest of the publish, which is
                # Slack API calls, stays unserialized.
                async with _home_tab_collect_gate():
                    all_rows = await _collect_recent_sessions_off_loop(
                        sess_mgr,
                        limit=per_kind * 10,
                        kind=(_SESSION_KIND_DASHBOARD, _SESSION_KIND_TASKRUNNER),
                    )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.home_tab_sessions_data_access",
                    outcome="allowed",
                    source="slack",
                    resources=f"{len(all_rows)} sessions read",
                )
                dashboard_rows = [r for r in all_rows if r["kind"] == _SESSION_KIND_DASHBOARD][
                    :per_kind
                ]
                taskrunner_rows = [r for r in all_rows if r["kind"] == _SESSION_KIND_TASKRUNNER][
                    :per_kind
                ]
                if dashboard_rows or taskrunner_rows:
                    if dashboard_rows:
                        blocks.append(
                            {
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": "*Main chat*"}],
                            }
                        )
                        blocks.extend(_build_sessions_blocks(dashboard_rows, for_home_tab=True))
                    if taskrunner_rows:
                        if dashboard_rows:
                            blocks.append({"type": "divider"})
                        blocks.append(
                            {
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": "*Task runner*"}],
                            }
                        )
                        blocks.extend(_build_sessions_blocks(taskrunner_rows, for_home_tab=True))
                else:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "_No recent sessions._"},
                        }
                    )
            except Exception as exc:
                # Redact-then-truncate the exception message before writing
                # it to SEL ``error=``, mirroring the slash and keyword
                # error-path patterns. SEL forwards externally redact, but
                # the on-disk audit file is not internally redacted, so this
                # is defense-in-depth for the security-controls
                # "never trust output" rule applied to exception messages.
                redacted_exc, _ = redact_exfiltration_urls(str(exc))
                redacted_exc, _ = redact_credentials(redacted_exc)
                logger.exception("home_tab sessions: collector failed for user %s", user_id)
                # SEL audit must record the access attempt even when the collector
                # raises, so a failure mode can't silently bypass the audit trail.
                # The success-path audit at the top of the try is skipped on
                # exception; this is the only audit that fires in that case.
                try:
                    sel().log_api_access(
                        caller=user_id,
                        operation="slack.home_tab_sessions_data_access",
                        outcome="error",
                        source="slack",
                        resources="0 sessions read (collector failed)",
                        error=redacted_exc[:200],
                    )
                except Exception:
                    logger.exception("Failed to emit SEL audit for home tab sessions error")
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "_Sessions unavailable._"},
                    }
                )
            blocks.append({"type": "divider"})

        # ── Recent Lessons ──
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": "📚 Recent Lessons"}}
        )
        lesson_lines: list[str] = []
        total_lessons = 0
        vs_ok = False
        # Primary: read from vector store (where learn_add writes).
        vs = getattr(orch, "vector_memory", None)
        if vs is not None and callable(getattr(vs, "get_lessons", None)):
            try:
                all_vs = await asyncio.to_thread(vs.get_lessons)
            except Exception:
                all_vs = None
                logger.debug("Vector store lesson read failed, trying JSONL", exc_info=True)
            if isinstance(all_vs, list):
                total_lessons = len(all_vs)
                # get_lessons() returns ORDER BY updated_at DESC (most recent first).
                # Deferred import: ``vector_memory`` pulls snowballstemmer plus
                # the optional numpy/faiss imports, and this renderer is the
                # module's only use of it, on an owner-command path.
                from kiro_crew.vector_memory import _lesson_display_text

                for entry in all_vs[:5]:
                    try:
                        parsed = json.loads(entry["value_json"])
                        # Rendered text for either storage shape, so a
                        # mapping-shaped row keeps its NOT-clause instead of
                        # showing rule-only (or a dict repr).  Credential
                        # redaction runs on the FULL display text (rule +
                        # negative combined) before truncation, so embedded
                        # secrets in either field are replaced.
                        rule = _lesson_display_text(parsed) or str(parsed)
                        safe = redact_credentials(redact_exfiltration_urls(rule)[0])[0]
                        lesson_lines.append(f"• {safe[:100]}")
                    except Exception:
                        logger.debug("Skipping malformed lesson entry", exc_info=True)
                vs_ok = True
        # Fallback: legacy JSONL store.
        if not vs_ok and orch.ctx_builder is not None:
            all_lessons = await asyncio.to_thread(orch.ctx_builder.lessons.load_all)
            total_lessons = len(all_lessons)
            for le in all_lessons[-5:]:
                lesson_lines.append(
                    f"• {redact_credentials(redact_exfiltration_urls(le.rule)[0])[0][:100]}"
                )
        if lesson_lines:
            if total_lessons > 5:
                lesson_lines.append(f"_…and {total_lessons - 5} more_")
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lesson_lines)}}
            )
        elif not vs_ok and orch.ctx_builder is None:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_Lessons unavailable._"}}
            )
        else:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_No lessons yet._"}}
            )
        blocks.append({"type": "divider"})

        # ── Commands ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "⌨️ Commands"}})
        _sc = f"/{orch.slack_command}"
        for name, (_, desc) in sorted(SLASH_REGISTRY.items()):
            blocks.append(command_hint_block(f"{_sc} {name}", desc))
        blocks.append(command_hint_block(f"{_sc} #channel", "track/untrack channel"))

        # ── Version ──
        version_text = f"📦 Kiro Crew v{__version__}"
        update_info = get_update_info()
        remote_ver = update_info.get("latest_version")
        if update_info.get("update_available") and remote_ver:
            version_text += f"  •  🆕 v{remote_ver} available — open Dashboard to update"
        version_text = redact_credentials(redact_exfiltration_urls(version_text)[0])[0]
        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": version_text}]})

        view = {"type": "home", "blocks": blocks}

        if orch.slack is not None:
            await orch.slack.views_publish(user_id=user_id, view=view)
        else:
            logger.warning("Cannot publish home tab — Slack client is None")

    except Exception:
        logger.error("Failed to publish home tab for %s", user_id, exc_info=True)
        # Attempt fallback error view
        try:
            if orch.slack is not None:
                fallback = {
                    "type": "home",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "⚠️ Failed to load Home Tab. Try again later.",
                            },
                        }
                    ],
                }
                await orch.slack.views_publish(user_id=user_id, view=fallback)
        except Exception:
            logger.debug("Fallback home tab also failed", exc_info=True)
