# Manual E2E Tests — Cron Notification → Chat Navigation

These tests require a running gateway and cannot be reliably automated due to
cron execution latency (scheduler polls every 30s + agent LLM processing time).

## Prerequisites

1. Stop production: `systemctl --user stop kirocrew.service`
2. Build frontend: `cd ../KiroCrewWebsite && npm run build`
3. Start gateway: `cd ../KiroCrew && kirocrew gateway`
4. Open dashboard at localhost:5476

## Test 1: One-Shot Cron → "View last result"

1. Create a one-shot cron (delay 10s, `persistent_session: true`)
2. Wait for notification (bell icon)
3. **Expected:** "View last result" button (no slot exists yet)
4. Click → navigates to chat with cron result visible

## Test 2: Recurring Cron → "Continue session" after first use

1. Create recurring cron (every 30s, `persistent_session: true`)
2. Wait for first notification → click "View last result" (creates slot)
3. Wait for second notification
4. **Expected:** "Continue session" button (slot now exists)
5. Click → navigates to existing chat slot with full history

## Test 3: Non-persistent Cron → "View last result" always

1. Create recurring cron (every 30s, `persistent_session: false`)
2. **Expected:** Always shows "View last result", never upgrades to "Continue session"

## Test 4: Context preserved in linked slot

1. After Test 2, open linked slot via "Continue session"
2. **Expected:** Full conversation history visible, can send messages, next cron result auto-injects

## Cleanup

```bash
systemctl --user start kirocrew.service
```

## Why not automated?

The cron scheduler polls every 30s (`_TIMER_POLL_SECS`), then the agent needs
10-30s to process the message. Total latency per cron fire is 40-90s, making
Playwright tests slow and flaky. The button rendering logic is covered by
deterministic integration tests in `integration/CronNotificationButtons.integration.test.tsx`.
