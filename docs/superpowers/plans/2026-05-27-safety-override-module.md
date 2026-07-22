# Safety Override Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace permanent YOLO mode with a time-limited safety override system backed by a dedicated module, eliminating dual-state sync and adding re-authorization + fleet governance.

**Architecture:** A new `SafetyOverride` singleton module owns all override state (activation, expiry, renewal). Slack handler and DashboardState delegate to it instead of maintaining independent globals. SEL audit covers every lifecycle event. `/api/status` gains two fields for fleet visibility.

**Tech Stack:** Python 3.9+ (dataclasses, time.monotonic), existing SEL module, aiohttp dashboard API, Slack Socket Mode handler.

---

## File Structure

| File | Responsibility |
|------|---------------|
| **Create:** `src/kiro_crew/safety_override.py` | Single source of truth for override state, TTLs, activation/renewal/expiry logic, SEL audit |
| **Create:** `test/test_safety_override.py` | Unit tests for the new module |
| **Modify:** `src/kiro_crew/slack/handler.py:410-934` | Remove YOLO globals and functions, delegate to `safety_override()` |
| **Modify:** `src/kiro_crew/slack/events.py:285-322` | Replace `_handle_yolo` to use `safety_override()`, add `renew` subcommand |
| **Modify:** `src/kiro_crew/dashboard/state.py:690-848` | Remove YOLO fields and methods, delegate to `safety_override()` |
| **Modify:** `src/kiro_crew/dashboard/chat_handlers.py:1204-1391` | Remove Slack sync block, use `safety_override()` |
| **Modify:** `src/kiro_crew/dashboard/chat_runner.py:730,1360` | Replace `state.is_yolo_active()` with `safety_override().is_active()` |
| **Modify:** `src/kiro_crew/dashboard/server.py:216-239` | Replace `_apply_startup_yolo` with `safety_override().activate("config")` |
| **Modify:** `src/kiro_crew/dashboard/handlers_system.py:115` | Add `yolo_active` and `yolo_expires_at` to `/api/status` |
| **Modify:** `src/kiro_crew/slack/gateway.py:326,368,2483-2488` | Replace `is_yolo_mode()` imports with `safety_override().is_active()` |
| **Modify:** `test/test_dashboard_yolo_startup.py` | Update tests to verify new 24h TTL behavior |

---

### Task 1: Create `SafetyOverride` Module — Core State & Activation

**Files:**
- Create: `src/kiro_crew/safety_override.py`
- Test: `test/test_safety_override.py`

- [ ] **Step 1: Write failing tests for activation and expiry**

```python
"""Tests for kiro_crew.safety_override module."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from kiro_crew.safety_override import SafetyOverride, OverrideStatus


@pytest.fixture
def override() -> SafetyOverride:
    """Fresh instance (bypass singleton for testing)."""
    inst = object.__new__(SafetyOverride)
    inst._active = False
    inst._source = ""
    inst._activated_at = 0.0
    inst._expires_at = 0.0
    inst._activation_count = 0
    inst._last_renewed_at = 0.0
    inst._last_renewed_by = ""
    inst._on_expired = None
    inst._on_activated = None
    return inst


class TestActivation:
    def test_activate_from_slack(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            result = override.activate("slack")
        assert result.active is True
        assert result.ttl == 1800
        assert override.is_active() is True
        mock_sel.return_value.log_api_access.assert_called_once()

    def test_activate_from_dashboard(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            result = override.activate("dashboard")
        assert result.ttl == 21600

    def test_activate_from_config(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            result = override.activate("config")
        assert result.ttl == 86400

    def test_activate_caps_at_max_ttl(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            result = override.activate("dashboard", ttl=200_000)
        assert result.ttl == 86400  # capped at _MAX_TTL

    def test_activate_fires_callback(self, override: SafetyOverride) -> None:
        called = []
        override._on_activated = lambda src, ttl: called.append((src, ttl))
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
        assert called == [("slack", 1800)]

    def test_activation_count_increments(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
            override.deactivate("slack")
            override.activate("dashboard")
        assert override._activation_count == 2


class TestExpiry:
    def test_is_active_returns_false_after_expiry(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
        # Simulate time passing beyond TTL
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel"):
            assert override.is_active() is False

    def test_expiry_fires_callback(self, override: SafetyOverride) -> None:
        called = []
        override._on_expired = lambda src: called.append(src)
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel"):
            override.is_active()
        assert called == ["slack"]

    def test_expiry_logs_sel_event(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            override.activate("slack")
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            override.is_active()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "safety_override:expired"


class TestDeactivation:
    def test_deactivate(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
            override.deactivate("slack")
        assert override.is_active() is False

    def test_deactivate_when_inactive_is_noop(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.deactivate("slack")  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_safety_override.py -v 2>&1 | head -40`
Expected: FAIL — `ModuleNotFoundError: No module named 'kiro_crew.safety_override'`

- [ ] **Step 3: Write the `safety_override.py` module**

```python
"""Time-limited safety override (YOLO mode) — single source of truth.

Replaces the scattered YOLO globals in slack/handler.py and dashboard/state.py.
All activations are capped at _MAX_TTL (24 hours). Re-authorization extends
the session. Every lifecycle event is SEL-audited.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_MAX_TTL = 86400  # 24 hours — hard ceiling
_SLACK_TTL = 1800  # 30 minutes
_DASHBOARD_TTL = 21600  # 6 hours
_CONFIG_TTL = 86400  # 24 hours (config-triggered startup)
_RENEW_GRACE_SECS = 300  # 5-minute grace window after expiry for renew()

_SOURCE_TTLS: dict[str, int] = {
    "slack": _SLACK_TTL,
    "dashboard": _DASHBOARD_TTL,
    "config": _CONFIG_TTL,
    "cli": _DASHBOARD_TTL,
}


@dataclass
class ActivationResult:
    active: bool
    ttl: int
    expires_at: float
    source: str
    error: str | None = None


@dataclass
class RenewResult:
    renewed: bool
    ttl: int
    expires_at: float
    source: str
    error: str | None = None


@dataclass
class OverrideStatus:
    active: bool
    source: str
    activated_at: str  # ISO 8601
    expires_at: str  # ISO 8601
    remaining_secs: int
    activation_count: int
    last_renewed_at: str | None
    last_renewed_by: str | None


class SafetyOverride:
    """Time-limited safety control override.

    All activations are capped at _MAX_TTL (24h). Re-authorization
    extends the current session without creating a new one.
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._source: str = ""
        self._activated_at: float = 0.0
        self._expires_at: float = 0.0
        self._activation_count: int = 0
        self._last_renewed_at: float = 0.0
        self._last_renewed_by: str = ""
        self._on_expired: Callable[[str], None] | None = None
        self._on_activated: Callable[[str, int], None] | None = None

    def activate(self, source: str, ttl: int | None = None) -> ActivationResult:
        """Activate the safety override with a time limit.

        Args:
            source: Origin interface ("slack", "dashboard", "config", "cli").
            ttl: Override duration in seconds. Defaults to source's preset.
                 Capped at _MAX_TTL regardless of input.
        """
        if ttl is None:
            ttl = _SOURCE_TTLS.get(source, _DASHBOARD_TTL)
        ttl = min(ttl, _MAX_TTL)

        self._active = True
        self._source = source
        self._activated_at = time.monotonic()
        self._expires_at = time.monotonic() + ttl
        self._activation_count += 1
        self._last_renewed_at = 0.0
        self._last_renewed_by = ""

        self._log_sel(
            operation="safety_override:activate",
            outcome="enabled",
            caller=source,
            resources=f"source:{source}, ttl:{ttl}s",
        )

        if self._on_activated:
            try:
                self._on_activated(source, ttl)
            except Exception:
                logger.warning("on_activated callback failed", exc_info=True)

        logger.info("Safety override activated (source=%s, ttl=%ds)", source, ttl)
        return ActivationResult(active=True, ttl=ttl, expires_at=self._expires_at, source=source)

    def renew(self, source: str) -> RenewResult:
        """Re-authorize an active (or recently-expired) override.

        Extends the TTL using the renewing source's default duration.
        Allows renewal within a 5-minute grace window after expiry.
        """
        now = time.monotonic()
        within_grace = (
            not self._active
            and self._expires_at > 0
            and (now - self._expires_at) <= _RENEW_GRACE_SECS
        )

        if not self._active and not within_grace:
            self._log_sel(
                operation="safety_override:renew",
                outcome="denied",
                caller=source,
                resources="reason:not_active",
            )
            return RenewResult(renewed=False, ttl=0, expires_at=0, source=source, error="not_active")

        ttl = min(_SOURCE_TTLS.get(source, _DASHBOARD_TTL), _MAX_TTL)
        self._active = True
        self._expires_at = now + ttl
        self._last_renewed_at = now
        self._last_renewed_by = source

        self._log_sel(
            operation="safety_override:renew",
            outcome="renewed",
            caller=source,
            resources=f"source:{source}, new_ttl:{ttl}s",
        )

        logger.info("Safety override renewed (source=%s, ttl=%ds)", source, ttl)
        return RenewResult(renewed=True, ttl=ttl, expires_at=self._expires_at, source=source)

    def deactivate(self, source: str) -> None:
        """Manually deactivate the safety override."""
        if not self._active:
            return
        self._active = False
        self._expires_at = 0.0

        self._log_sel(
            operation="safety_override:deactivate",
            outcome="disabled",
            caller=source,
            resources=f"source:{source}",
        )
        logger.info("Safety override deactivated (source=%s)", source)

    def is_active(self) -> bool:
        """Check if override is currently active, triggering expiry if needed."""
        if not self._active:
            return False
        if time.monotonic() > self._expires_at:
            self._expire()
            return False
        return True

    def remaining_secs(self) -> int:
        """Seconds until expiry. Returns 0 if inactive."""
        if not self.is_active():
            return 0
        return max(0, int(self._expires_at - time.monotonic()))

    def status(self) -> OverrideStatus:
        """Rich status for API and governance endpoints."""
        active = self.is_active()
        remaining = self.remaining_secs() if active else 0
        now_ts = time.time()
        activated_iso = ""
        expires_iso = ""
        if self._activated_at > 0:
            wall_activated = now_ts - (time.monotonic() - self._activated_at)
            activated_iso = datetime.fromtimestamp(wall_activated, tz=timezone.utc).isoformat()
        if active and self._expires_at > 0:
            wall_expires = now_ts + (self._expires_at - time.monotonic())
            expires_iso = datetime.fromtimestamp(wall_expires, tz=timezone.utc).isoformat()

        renewed_iso = None
        if self._last_renewed_at > 0:
            wall_renewed = now_ts - (time.monotonic() - self._last_renewed_at)
            renewed_iso = datetime.fromtimestamp(wall_renewed, tz=timezone.utc).isoformat()

        return OverrideStatus(
            active=active,
            source=self._source,
            activated_at=activated_iso,
            expires_at=expires_iso,
            remaining_secs=remaining,
            activation_count=self._activation_count,
            last_renewed_at=renewed_iso,
            last_renewed_by=self._last_renewed_by or None,
        )

    def _expire(self) -> None:
        """Handle expiry: deactivate, audit, notify."""
        source = self._source
        self._active = False

        self._log_sel(
            operation="safety_override:expired",
            outcome="expired",
            caller="system",
            resources=f"source:{source}",
        )
        logger.info("Safety override expired (source=%s)", source)

        if self._on_expired:
            try:
                self._on_expired(source)
            except Exception:
                logger.warning("on_expired callback failed", exc_info=True)

    def _log_sel(self, *, operation: str, outcome: str, caller: str, resources: str) -> None:
        """Emit SEL audit event."""
        try:
            from kiro_crew.sel import sel
            sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome=outcome,
                source="safety_override",
                resources=resources,
            )
        except Exception:
            logger.warning("SEL audit failed for %s", operation, exc_info=True)


# Module-level singleton
_instance: SafetyOverride | None = None


def safety_override() -> SafetyOverride:
    """Get the global SafetyOverride singleton."""
    global _instance
    if _instance is None:
        _instance = SafetyOverride()
    return _instance


def reset_singleton() -> None:
    """Reset singleton (testing only)."""
    global _instance
    _instance = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_safety_override.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/kiro_crew/safety_override.py test/test_safety_override.py
git commit -m "feat(security): add SafetyOverride module with time-limited YOLO

Implements the YOLO override governance work: replaces permanent YOLO with capped overrides.
- 24h max TTL (config), 6h (dashboard), 30min (Slack)
- Renewal with 5-min grace window after expiry
- SEL audit on every lifecycle event (activate/renew/expire/deactivate)
- Callbacks for expiry notifications"
```

---

### Task 2: Add Renewal Tests

**Files:**
- Modify: `test/test_safety_override.py`

- [ ] **Step 1: Add renewal and status tests**

Append to `test/test_safety_override.py`:

```python
class TestRenewal:
    def test_renew_active_override(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
            result = override.renew("dashboard")
        assert result.renewed is True
        assert result.ttl == 21600  # dashboard TTL on renew

    def test_renew_within_grace_period(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
        # Expire it just barely (within 5-min grace)
        override._expires_at = time.monotonic() - 60  # 1 min ago
        override._active = False
        with patch("kiro_crew.safety_override.sel"):
            result = override.renew("slack")
        assert result.renewed is True
        assert override.is_active() is True

    def test_renew_outside_grace_period_fails(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("slack")
        # Expire it well past grace window
        override._expires_at = time.monotonic() - 600  # 10 min ago
        override._active = False
        with patch("kiro_crew.safety_override.sel"):
            result = override.renew("slack")
        assert result.renewed is False
        assert result.error == "not_active"

    def test_renew_logs_sel(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("dashboard")
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            override.renew("dashboard")
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "safety_override:renew"
        assert kwargs["outcome"] == "renewed"


class TestStatus:
    def test_status_when_active(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel"):
            override.activate("dashboard")
        st = override.status()
        assert st.active is True
        assert st.source == "dashboard"
        assert st.remaining_secs > 0
        assert st.activation_count == 1

    def test_status_when_inactive(self, override: SafetyOverride) -> None:
        st = override.status()
        assert st.active is False
        assert st.remaining_secs == 0
```

- [ ] **Step 2: Run tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_safety_override.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add test/test_safety_override.py
git commit -m "test(safety_override): add renewal and status tests"
```

---

### Task 3: Integrate Into Dashboard State

**Files:**
- Modify: `src/kiro_crew/dashboard/state.py:690-848`
- Modify: `src/kiro_crew/dashboard/chat_runner.py:730,1360`

- [ ] **Step 1: Replace YOLO fields in `state.py`**

In `src/kiro_crew/dashboard/state.py`, remove lines 690-692 (`_yolo`, `_yolo_expires_at`, `_yolo_from_config` fields from `__init__`).

Remove lines 799-848 (the `_YOLO_TTL` constant and `enable_yolo`, `disable_yolo`, `_expire_yolo_if_needed`, `is_yolo_active` methods).

Replace with delegating methods:

```python
    # -- In __init__, remove these 3 lines:
    # self._yolo: bool = False
    # self._yolo_expires_at: float = 0.0
    # self._yolo_from_config: bool = False

    # -- Replace the removed methods with:
    def enable_yolo(self, *, from_config: bool = False) -> None:
        """Activate safety override (delegates to safety_override module)."""
        from kiro_crew.safety_override import safety_override
        source = "config" if from_config else "dashboard"
        safety_override().activate(source)

    def disable_yolo(self) -> None:
        """Deactivate safety override (delegates to safety_override module)."""
        from kiro_crew.safety_override import safety_override
        safety_override().deactivate("dashboard")

    def is_yolo_active(self) -> bool:
        """Return whether safety override is active (delegates to safety_override module)."""
        from kiro_crew.safety_override import safety_override
        return safety_override().is_active()
```

- [ ] **Step 2: Update `chat_runner.py` references**

In `src/kiro_crew/dashboard/chat_runner.py` at line 730 and 1360, `state.is_yolo_active()` continues to work as-is since we kept the delegating method. No changes needed here.

- [ ] **Step 3: Run existing YOLO startup tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_dashboard_yolo_startup.py -v`
Expected: May need adjustment (test expects `state.is_yolo_active()` to work — it will since we kept the delegation)

- [ ] **Step 4: Commit**

```bash
git add src/kiro_crew/dashboard/state.py
git commit -m "refactor(state): delegate YOLO to safety_override module

DashboardState.enable_yolo/disable_yolo/is_yolo_active now delegate
to the SafetyOverride singleton. Internal _yolo* fields removed."
```

---

### Task 4: Integrate Into Slack Handler

**Files:**
- Modify: `src/kiro_crew/slack/handler.py:410-934`
- Modify: `src/kiro_crew/slack/events.py:285-322`
- Modify: `src/kiro_crew/slack/gateway.py:326,368,2483-2488`

- [ ] **Step 1: Replace Slack handler globals and functions**

In `src/kiro_crew/slack/handler.py`:

Remove lines 410-418 (YOLO state globals):
```python
# Remove:
# _yolo_mode = False
# _yolo_expires_at: float = 0.0
# _yolo_from_config: bool = False
# _YOLO_TTL_SECS = 1800
# _YOLO_DASHBOARD_TTL_SECS = 21600
```

Replace `set_yolo_mode()` (line 809-814) with:
```python
def set_yolo_mode(enabled: bool) -> None:
    """Set YOLO mode at startup from config (called by gateway)."""
    if enabled:
        from kiro_crew.safety_override import safety_override
        safety_override().activate("config")
```

Replace `disable_yolo()` (line 887-895) with:
```python
def disable_yolo() -> None:
    """Disable YOLO mode (global auto-approve)."""
    from kiro_crew.safety_override import safety_override
    safety_override().deactivate("slack")
    _trusted_sessions.clear()
    logger.info("YOLO mode OFF")
```

Replace `enable_yolo_with_ttl()` (line 901-913) with:
```python
def enable_yolo_with_ttl(ttl_secs: int) -> None:
    """Enable YOLO mode with a specific TTL."""
    from kiro_crew.safety_override import safety_override
    safety_override().activate("slack", ttl=ttl_secs)
    logger.info("YOLO mode ON (expires in %ds)", ttl_secs)
```

Replace `is_yolo_mode()` (line 916-934) with:
```python
def is_yolo_mode() -> bool:
    """Return whether YOLO mode is currently active."""
    from kiro_crew.safety_override import safety_override
    return safety_override().is_active()
```

Keep `_YOLO_TTL_SECS = 1800` and `_YOLO_DASHBOARD_TTL_SECS = 21600` as constants (they're still referenced in messages and by other imports).

- [ ] **Step 2: Update `!yolo` command handler (handler.py:1043-1087)**

Replace the `_yolo_from_config` check at line 1061 with:
```python
            if _yolo_from_config:
```
→ Change to:
```python
            from kiro_crew.safety_override import safety_override
            if safety_override().is_active() and safety_override()._source == "config":
```

Actually, simpler: keep a module-level helper:
```python
# Near top of yolo command block:
_yolo_from_config = False  # Remove this global; replace check with:
```
Replace the block at line 1061:
```python
            elif not yolo_active:
```
(The `_yolo_from_config` check can be removed entirely — `is_yolo_mode()` returning True already handles the "already on" case at line 1080.)

Simplify the `!yolo on` block to:
```python
        elif len(parts) >= 2 and parts[1].lower() == "on":
            if not yolo_active:
                enable_yolo_with_ttl(_YOLO_TTL_SECS)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="allowed",
                    source="slack",
                    resources="yolo_on",
                )
                await slack.post_message(channel, f"🔓 YOLO mode enabled (auto-expires in {_YOLO_TTL_SECS // 60}min).", reply_ts)
            else:
                remaining = safety_override().remaining_secs()
                await slack.post_message(channel, f"YOLO mode is already on ({remaining // 60}min remaining).", reply_ts)
```

- [ ] **Step 3: Add `!yolo renew` command**

In the `!yolo` command handler (after the `elif parts[1].lower() == "on"` block), add:
```python
        elif len(parts) >= 2 and parts[1].lower() == "renew":
            from kiro_crew.safety_override import safety_override
            result = safety_override().renew("slack")
            if result.renewed:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="renewed",
                    source="slack",
                    resources="yolo_renew",
                )
                await slack.post_message(channel, f"🔓 YOLO mode renewed (auto-expires in {result.ttl // 60}min).", reply_ts)
            else:
                await slack.post_message(channel, "YOLO mode is not active. Use `!yolo on` to activate.", reply_ts)
```

- [ ] **Step 4: Update `events.py:_handle_yolo`**

In `src/kiro_crew/slack/events.py`, replace `_handle_yolo` (lines 285-322):

```python
async def _handle_yolo(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Toggle YOLO mode on/off/renew."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can toggle YOLO mode.")
        return

    from kiro_crew.safety_override import safety_override

    arg = args.strip().lower()
    if arg == "on":
        so = safety_override()
        if so.is_active():
            remaining = so.remaining_secs()
            await respond(f"🟢 YOLO mode is already *ON* ({remaining // 60}min remaining).")
            return
        so.activate("slack")
        sel().log_api_access(caller=caller_id, operation="slack.yolo_mode", outcome="allowed", source="slack", resources="yolo_on")
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond(f"🟢 YOLO mode *ON* (auto-expires in {_YOLO_TTL_SECS // 60}min) — all tools auto-approved.")
    elif arg == "off":
        safety_override().deactivate("slack")
        sel().log_api_access(caller=caller_id, operation="slack.yolo_mode", outcome="allowed", source="slack", resources="yolo_off")
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond("🔴 YOLO mode *OFF* — tools require approval.")
    elif arg == "renew":
        result = safety_override().renew("slack")
        if result.renewed:
            sel().log_api_access(caller=caller_id, operation="slack.yolo_mode", outcome="renewed", source="slack", resources="yolo_renew")
            if orch.dashboard_state:
                orch.dashboard_state.push_slots_update()
            await respond(f"🟢 YOLO mode *renewed* (auto-expires in {result.ttl // 60}min).")
        else:
            await respond("🔴 YOLO mode is not active. Use `on` to activate first.")
    else:
        so = safety_override()
        if so.is_active():
            remaining = so.remaining_secs()
            await respond(f"YOLO mode is currently *ON 🟢* ({remaining // 60}min remaining).\nUsage: `/{orch.slack_command} yolo on|off|renew`")
        else:
            await respond(f"YOLO mode is currently *OFF 🔴*.\nUsage: `/{orch.slack_command} yolo on|off|renew`")
```

- [ ] **Step 5: Update `gateway.py` references**

In `src/kiro_crew/slack/gateway.py`:

At line 326 and 2483, replace:
```python
from kiro_crew.slack.handler import is_yolo_mode
```
with:
```python
from kiro_crew.safety_override import safety_override
```

At line 368 replace `if is_yolo_mode():` with `if safety_override().is_active():`.
At line 2488 replace `return is_yolo_mode()` with `return safety_override().is_active()`.

- [ ] **Step 6: Run tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_safety_override.py test/test_dashboard_yolo_startup.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/kiro_crew/slack/handler.py src/kiro_crew/slack/events.py src/kiro_crew/slack/gateway.py
git commit -m "refactor(slack): delegate YOLO state to safety_override module

- handler.py: globals removed, functions delegate to SafetyOverride
- events.py: _handle_yolo supports 'renew' subcommand
- gateway.py: uses safety_override().is_active() directly"
```

---

### Task 5: Remove Dashboard ↔ Slack Sync Block

**Files:**
- Modify: `src/kiro_crew/dashboard/chat_handlers.py:1313-1331`

- [ ] **Step 1: Remove the sync block**

In `src/kiro_crew/dashboard/chat_handlers.py`, remove lines 1313-1331:

```python
    # Remove this entire block:
    # Sync Slack handler YOLO state with dashboard
    from kiro_crew.slack.handler import (
        _YOLO_DASHBOARD_TTL_SECS,
        disable_yolo,
        enable_yolo_with_ttl,
        is_yolo_mode,
    )

    if mode == "yolo" and not is_yolo_mode():
        enable_yolo_with_ttl(_YOLO_DASHBOARD_TTL_SECS)
        sel().log_api_access(
            caller="dashboard",
            operation="dashboard.yolo_mode",
            outcome="allowed",
            source="dashboard",
            resources="yolo_on",
        )
    elif mode != "yolo" and is_yolo_mode():
        disable_yolo()
```

This sync is no longer needed — both Slack and Dashboard read from the same `SafetyOverride` singleton.

- [ ] **Step 2: Update `state.enable_yolo()` call at line 1224**

Replace:
```python
        state.enable_yolo()  # TTL enforced internally (state._YOLO_TTL)
```
with:
```python
        from kiro_crew.safety_override import safety_override
        safety_override().activate("dashboard")
```

- [ ] **Step 3: Update `state.disable_yolo()` calls at lines 1235, 1253, 1284**

Replace all `state.disable_yolo()` with:
```python
        from kiro_crew.safety_override import safety_override
        safety_override().deactivate("dashboard")
```

(Import once at the top of the function instead of repeating.)

- [ ] **Step 4: Update policy propagation at line 1387**

Replace:
```python
        policy = "auto" if slot._trust or state.is_yolo_active() else ""
```
with:
```python
        from kiro_crew.safety_override import safety_override
        policy = "auto" if slot._trust or safety_override().is_active() else ""
```

- [ ] **Step 5: Run tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_dashboard_yolo_startup.py test/test_safety_override.py test/test_dashboard_approval.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/kiro_crew/dashboard/chat_handlers.py
git commit -m "refactor(chat_handlers): remove Slack↔Dashboard YOLO sync

Both interfaces now read from SafetyOverride singleton.
The manual sync block is eliminated."
```

---

### Task 6: Update Dashboard Startup (`_apply_startup_yolo`)

**Files:**
- Modify: `src/kiro_crew/dashboard/server.py:216-239`
- Modify: `test/test_dashboard_yolo_startup.py`

- [ ] **Step 1: Replace `_apply_startup_yolo`**

In `src/kiro_crew/dashboard/server.py`, replace the function (lines 216-239):

```python
def _apply_startup_yolo(state: DashboardState, cfg: Any) -> None:
    """Enable safety override at startup if ``agent.yolo=true`` in config.

    Activates with 24h TTL (no longer permanent). Re-auth required after expiry.
    """
    if not cfg.agent.yolo:
        return
    from kiro_crew.safety_override import safety_override
    try:
        result = safety_override().activate("config")
    except Exception:
        logger.error("Failed to activate safety override from config", exc_info=True)
        return
    logger.info(
        "Safety override enabled at startup (agent.yolo=true, expires in %ds)",
        result.ttl,
    )
```

- [ ] **Step 2: Update tests in `test_dashboard_yolo_startup.py`**

Replace the entire test file:

```python
"""Tests that ``agent.yolo=true`` enables time-limited safety override at startup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiro_crew.dashboard.server import _apply_startup_yolo
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.safety_override import reset_singleton


def _make_state() -> DashboardState:
    return DashboardState(
        sessions=MagicMock(),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _cfg(yolo: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(yolo=yolo))


def setup_function() -> None:
    reset_singleton()


def teardown_function() -> None:
    reset_singleton()


def test_apply_startup_yolo_enables_with_24h_ttl() -> None:
    """agent.yolo=true activates safety override with 24h cap."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=True))

    from kiro_crew.safety_override import safety_override
    so = safety_override()
    assert so.is_active() is True
    assert so._source == "config"
    # TTL should be 24h (86400s), check remaining is close
    assert so.remaining_secs() > 86000


def test_apply_startup_yolo_noop_when_config_false() -> None:
    """agent.yolo=false does not activate override."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=False))

    from kiro_crew.safety_override import safety_override
    assert safety_override().is_active() is False


def test_apply_startup_yolo_logs_sel() -> None:
    """Activation emits SEL audit event."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        _apply_startup_yolo(state, _cfg(yolo=True))

    mock_sel.return_value.log_api_access.assert_called()
    kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
    assert kwargs["operation"] == "safety_override:activate"
    assert kwargs["outcome"] == "enabled"
```

- [ ] **Step 3: Run tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_dashboard_yolo_startup.py test/test_safety_override.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/kiro_crew/dashboard/server.py test/test_dashboard_yolo_startup.py
git commit -m "feat(startup): agent.yolo=true now activates 24h capped override

No longer permanent — requires re-authorization after 24h.
SEL audit event emitted on activation."
```

---

### Task 7: Add Fleet Governance Endpoints

**Files:**
- Modify: `src/kiro_crew/dashboard/handlers_system.py:106-122`
- Modify: `src/kiro_crew/dashboard/server.py` (route registration)

- [ ] **Step 1: Add `yolo_active` and `yolo_expires_at` to `/api/status`**

In `src/kiro_crew/dashboard/handlers_system.py`, after line 115 (`"yolo": state._yolo,`), replace with:

```python
        from kiro_crew.safety_override import safety_override
        so = safety_override()
        so_status = so.status()
```

Then in the `data.update(...)` block, replace `"yolo": state._yolo,` with:
```python
        "yolo": so_status.active,
        "yolo_active": so_status.active,
        "yolo_expires_at": so_status.expires_at,
        "yolo_remaining_secs": so_status.remaining_secs,
```

- [ ] **Step 2: Add `/api/admin/compliance/yolo-status` endpoint**

Add a new handler in `src/kiro_crew/dashboard/handlers_system.py`:

```python
async def api_compliance_yolo_status(request: web.Request) -> web.Response:
    """GET /api/admin/compliance/yolo-status — safety override governance status."""
    from kiro_crew.safety_override import safety_override
    from dataclasses import asdict
    status = safety_override().status()
    return web.json_response(asdict(status))
```

- [ ] **Step 3: Register the route**

In `src/kiro_crew/dashboard/server.py`, add near the other admin/system routes:
```python
    app.router.add_get("/api/admin/compliance/yolo-status", handlers.api_compliance_yolo_status)
```

- [ ] **Step 4: Run tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_safety_override.py test/test_dashboard_yolo_startup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kiro_crew/dashboard/handlers_system.py src/kiro_crew/dashboard/server.py
git commit -m "feat(governance): add yolo_active/expires_at to /api/status

Adds /api/admin/compliance/yolo-status for full override status.
Fleet monitoring tools get override visibility via existing status poll."
```

---

### Task 8: Wire Expiry Notifications

**Files:**
- Modify: `src/kiro_crew/dashboard/server.py` (wire `on_expired` callback)
- Modify: `src/kiro_crew/slack/gateway.py` (Slack expiry notification)

- [ ] **Step 1: Wire the `on_expired` callback in gateway startup**

In `src/kiro_crew/dashboard/server.py`, after the `_apply_startup_yolo` call, add:

```python
    from kiro_crew.safety_override import safety_override

    def _on_override_expired(source: str) -> None:
        """Notify dashboard clients when safety override expires."""
        state.broadcast_ws("yolo_expired", {"source": source})
        state.push_slots_update()
        # Clear approval policies for non-trusted slots
        for slot in state._slots.values():
            if not slot._trust and not slot._trust_reads:
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")

    safety_override().on_expired = _on_override_expired
```

- [ ] **Step 2: Wire Slack expiry notification in gateway**

In `src/kiro_crew/slack/gateway.py`, during gateway initialization (where heartbeat or on_expired would be wired), add a Slack notification path. The simplest approach: in the existing `on_expired` callback, also post to Slack:

Update the `_on_override_expired` in `server.py` to also notify Slack:

```python
    def _on_override_expired(source: str) -> None:
        """Notify all interfaces when safety override expires."""
        # Dashboard notification
        state.broadcast_ws("yolo_expired", {"source": source})
        state.push_slots_update()
        for slot in state._slots.values():
            if not slot._trust and not slot._trust_reads:
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
        # Slack notification (fire-and-forget)
        try:
            from kiro_crew.slack.handler import _post_to_owner
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_post_to_owner(
                    "🔒 Safety override expired. Tools now require approval. Reply `!yolo renew` to re-authorize."
                ))
        except Exception:
            logger.debug("Slack expiry notification skipped", exc_info=True)
```

- [ ] **Step 3: Add `_post_to_owner` helper in Slack handler (if it doesn't exist)**

In `src/kiro_crew/slack/handler.py`, add:

```python
async def _post_to_owner(text: str) -> None:
    """Post a message to the owner's DM (for system notifications)."""
    if not _owner_id or not _slack:
        return
    try:
        await _slack.post_dm(_owner_id, text)
    except Exception:
        logger.debug("Failed to post to owner DM", exc_info=True)
```

(Check if `_slack.post_dm` exists or use `_slack.post_message` with the owner's DM channel.)

- [ ] **Step 4: Run tests**

Run: `cd /path/to/KiroCrew && python -m pytest test/test_safety_override.py test/test_dashboard_yolo_startup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kiro_crew/dashboard/server.py src/kiro_crew/slack/handler.py src/kiro_crew/slack/gateway.py
git commit -m "feat(notifications): wire expiry notifications to Dashboard + Slack

When safety override expires:
- Dashboard receives 'yolo_expired' WebSocket event (banner trigger)
- Slack owner gets DM: 'override expired, reply !yolo renew'
- Approval policies cleared for non-trusted slots"
```

---

### Task 9: Clean Up Legacy References & Run Full Test Suite

**Files:**
- Modify: `src/kiro_crew/slack/handler.py` (remove dead `_yolo_from_config` references)
- Modify: `src/kiro_crew/dashboard/chat_handlers.py:1461` (remaining `state.enable_yolo()`)

- [ ] **Step 1: Audit remaining legacy references**

Run: `grep -rn "_yolo_from_config\|_yolo_mode\|_yolo_expires_at\|_yolo_active_ttl" src/kiro_crew/ --include="*.py" | grep -v __pycache__`

Fix any remaining direct references to the old globals.

- [ ] **Step 2: Fix `chat_handlers.py:1461`**

If there's a remaining `state.enable_yolo()` at line 1461, replace with:
```python
        from kiro_crew.safety_override import safety_override
        safety_override().activate("dashboard")
```

- [ ] **Step 3: Run full test suite**

Run: `cd /path/to/KiroCrew && python -m pytest test/ -x --timeout=60 -q 2>&1 | tail -30`
Expected: All tests pass (or only unrelated failures)

- [ ] **Step 4: Run type checking**

Run: `cd /path/to/KiroCrew && python -m mypy src/kiro_crew/safety_override.py --ignore-missing-imports`
Expected: No errors

- [ ] **Step 5: Commit final cleanup**

```bash
git add -A
git commit -m "chore: clean up legacy YOLO globals and fix remaining references"
```

---

### Task 10: Update Security Design Document

**Files:**
- Modify: `docs/security-deep-dive.md`

- [ ] **Step 1: Update the YOLO section in security-deep-dive.md**

Find the section that describes YOLO/approval mode and update to reflect:
- All activations are now time-limited (max 24h)
- No permanent mode exists
- Re-authorization flow with 5-minute grace window
- SEL events: `safety_override:activate`, `safety_override:renew`, `safety_override:expired`, `safety_override:deactivate`
- Fleet governance via `/api/status` fields and `/api/admin/compliance/yolo-status`

- [ ] **Step 2: Commit**

```bash
git add docs/security-deep-dive.md
git commit -m "docs(security): update YOLO section for time-limited override

Reflects the YOLO override governance work: no permanent mode, 24h cap, re-auth flow,
fleet governance endpoints."
```
