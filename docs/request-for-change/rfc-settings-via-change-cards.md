---
title: Every Settings row reachable from chat through a change card
status: draft
author: lummi-ch23, with a kirocrew agent session
created: 2026-11-24
last-audited: 2026-11-24
audited-at: 4172c08404
doc-pr: null
implementation-prs: []
tracking-issues: [23179]
supersedes: []
superseded-by: []
---

# RFC: Every Settings row reachable from chat through a change card

- Status: draft. Nothing on main follows it and it changes no code. It asks a maintainer to accept the proposed design below. A maintainer records acceptance on this Status line, in the `status` field and in the index row.
- Tracking issue: [#23179](https://github.com/kirodotdev/KiroCrew/issues/23179) (labels `tier:T3` and `parked`). This document is the one-pager a T3 change needs. The reload and scope part of the issue stays with [#10211](https://github.com/kirodotdev/KiroCrew/issues/10211).
- Main was read at `4172c08404`. Every number below was measured there; the method is in [Measurement](#measurement).

## Summary

A user who says "turn on session summaries" or "connect new sessions to Slack" should get one change card that applies it with one click. Today the agent can write 57 of the 357 rows the Settings page shows. For the other 300, `find_setting` answers `writable: false` and the agent can only point at the menu.

This RFC proposes:

1. a write-path class for every Settings row, checked by a test, so a new row without a path fails CI;
2. closing the gaps in order of cost: rows whose write route already exists first, browser-local and desktop-local rows next, the channel tabs last;
3. a reason code on every row that stays out of reach, reusing the reason codes the Advanced page already has;
4. remove operations where a card can only add or toggle today.

Safety stays as it is: the card is the confirmation, it keeps its risk label, and safety-sensitive rows keep their stricter path.

## Motivation

### Current state

Verified at `4172c08404`:

1. **The registry.** `website/scripts/gen-settings-registry.mjs` extracts every Settings panel control into `src/kiro_crew/docs/settings-registry.generated.json`. It holds 357 rows across 19 tabs. A row carries a `configKey` only when its JSX names one (`extractStringProp(props, 'configKey')` in `website/scripts/settingsExtract.ts`); 258 rows have none.
2. **What a card may write.** `resolve_setting` in `src/kiro_crew/dashboard/change_cards_settings.py` resolves a registry id to a write target in exactly three ways: the id is in `DASHBOARD_SETTINGS` or `DASHBOARD_LIST_SETTINGS` (`src/kiro_crew/change_card_catalog_plans.py`, 15 and 1 entries), or its `configKey` is in `_EDITABLE_CONFIG` (`src/kiro_crew/dashboard/handlers/core_config_schema.py`, 144 keys), or its `configKey` is `dashboard.<key>` with `<key>` in `DASHBOARD_KEYS`. Anything else raises `no_write_path`, and `find_settings` then reports `writable: false`.
3. **The result.** 57 rows resolve. 300 raise `no_write_path`: 258 have no `configKey`, and 42 have one that no card table lists (14 `dashboard.*`, 14 `stt.*`, 8 `slack.*`, 4 `teams.*`, 2 `mcp.*`). Both examples in the issue are in the second group: "Connect new sessions to Slack automatically" is `slack.auto_link_sessions` and "New Session Folders" is `dashboard.session_folder_root`.
4. **Write routes that already exist.** 11 of the 14 unlisted `dashboard.*` keys appear in `api_dashboard_config` (`src/kiro_crew/dashboard/file_api/dashboard_config.py`), the route `DASHBOARD_SETTINGS` already writes through; `session_folder_root` is one of them. `slack.auto_link_sessions` is saved by `api_slack_config_save` in `src/kiro_crew/dashboard/messaging_api/slack_settings.py`. The Voice tab saves through `src/kiro_crew/dashboard/handlers/core_stt_settings.py`, and each channel has its own module under `src/kiro_crew/dashboard/messaging_api/` (for example `teams_settings.py`, `discord_settings.py`).
5. **Stores the gateway does not hold.** Some rows live only in the browser or the desktop app. `website/src/hooks/usePersistedBool.ts` and `usePersistedString.ts` keep a value in `localStorage`; 19 call sites in 10 files under `website/src/pages/settings` read or write it that way. The desktop app's window settings go through `website/src/hooks/useMainWindowSetting.ts` to the Electron preload bridge (`useMinimizeToTray.ts` is one user).
6. **The change kinds.** `KINDS` in `src/kiro_crew/change_card_catalog_kinds.py` has 24 kinds. `list_kinds` hides 5 of them from `list_change_kinds` (`APP_ONLY_KINDS`, `SELF_ONLY_KINDS`, `GATEWAY_RESOLVED_KINDS`), so an agent sees 19.
7. **Remove operations.** The issue says a custom MCP server cannot be deleted from a card. That is out of date: `request_server` (`src/kiro_crew/mcp_tools/catalog/server_request.py`) takes `action` `remove` and raises the `mcp.server` card (`src/kiro_crew/change_card_mcp_server.py`), which calls the MCP page's own uninstall. It is not in `list_change_kinds`, because `mcp.server` is in `GATEWAY_RESOLVED_KINDS`, so an agent that only reads that list does not find it. `denied_command` takes `action` `add` or `toggle` only; no card removes a rule.
8. **The Advanced page's reasons.** `src/kiro_crew/dashboard/handlers/config_all_settings.py` already explains, per config key, why the Advanced page will not change it: `WHY_SAFETY`, `WHY_SIGN_IN`, `WHY_REACH`, `WHY_SHARE`, `WHY_OWNED`, `WHY_MARKER`, `WHY_ALIAS`, `WHY_TEXT`, `WHY_LOCKED` and `WHY_COMPOUND` in `READONLY_REASONS`. Its docstring says `generic_spec` rules are never added to `_EDITABLE_CONFIG`, so a card cannot propose those keys. `mcp.honour_auto_approve` and `mcp.protect_kiro_permission_rules`, the two unlisted `mcp.*` rows, are `WHY_SAFETY` there.
9. **No coverage guard.** No test under `test/` asserts that a registry row has a card path. `test/test_change_cards_settings.py` tests `find_settings` ranking and the list setting, not coverage.
10. **The menu fallback.** For a non-writable row the error text tells the agent to offer the guide's `settings.show` action (`ACTION_SETTINGS_SHOW` in `src/kiro_crew/guide_catalog.py`).

### Problem

A non-technical user's model is "I tell Kiro and Kiro does it". For 300 of 357 rows the agent can only send that user to a menu. The reason is not one missing feature: the rows sit in at least five stores (gateway config through `_EDITABLE_CONFIG`, the dashboard config route, per-area routes such as Slack and Voice, browser `localStorage`, the desktop app), and each card table is a hand-kept allowlist. Nothing fails when a new row ships without a path, so the gap grows with every Settings change.

## Goals

- Every Settings row is in exactly one write-path class, and a test enforces it.
- A row whose page writes through an existing route gets a card that calls that same route.
- `find_setting` never answers a bare `writable: false`: it names the card to use or the reason there is none.
- A card can remove what it can add, where the page can.

## Non-goals

- Reloading or rescoping open chats after a change. That is [#10211](https://github.com/kirodotdev/KiroCrew/issues/10211).
- Loosening any safety switch. A row that is `WHY_SAFETY`, `WHY_SIGN_IN` or `WHY_REACH` today does not become a plain `setting.change`.
- Removing menus. The Settings page stays as it is.

## Proposed design

### D1. One write-path class per row, checked by a test

Each registry row is proposed to carry one class:

| Class | Meaning | Card |
|---|---|---|
| `card` | `resolve_setting` returns a target | `setting.change` |
| `route` | the page saves through a gateway route other than the two `setting.change` uses today | `setting.change` with a route target (D2) |
| `client` | the value lives in the browser or the desktop app | `setting.change` applied in the client (D3) |
| `kind` | a dedicated kind owns it | that kind, named in `find_setting` (for example `secret.save`, `denied_command`, `connection.connect`) |
| `strict` | a `READONLY_REASONS` code applies | none, or a stricter dedicated kind if a maintainer accepts one; `find_setting` returns the code |
| `info` | the row shows a value and changes nothing (for example `about.version`, `about.release-notes`) | none |

The class is proposed to come from the panel source, like `configKey` does: the extractor in `website/scripts/settingsExtract.ts` reads it from the row's props, so the agent registry and the page keep one source. A Python test then loads `settings-registry.generated.json` and fails for a row with no class, and for a `card` or `route` row that `resolve_setting` refuses.

The 300 rows unresolved today go into a checked-in baseline file that may only shrink, the same pattern as the repository's other `*-baseline.json` files. A new row without a class fails at once; old rows leave the baseline as each phase lands.

### D2. Route targets for rows whose route already exists

`resolve_setting` today knows two stores, `STORE_KIROCREW` and `STORE_DASHBOARD`. The proposal adds a route target: the card's Apply calls the same route and body key the Settings control sends, as `mcp.server` already does for the MCP page. Concretely, in order:

1. The 11 `dashboard.*` keys that `api_dashboard_config` already accepts go into `DASHBOARD_SETTINGS` with the values their controls offer. This needs no new store.
2. `slack.*`, `teams.*` and `stt.*` rows get route targets on `api_slack_config_save`, the Teams settings module and `core_stt_settings.py`. The route keeps its own checks; for example the Slack route refuses an edit that `config.local.json` pins, and the card shows that refusal instead of a false success.
3. The 3 remaining `dashboard.*` keys (`milestones_enabled`, `theme_schedule_dark`, `theme_schedule_light`) are classified after their write path is read; the grep in [Measurement](#measurement) did not find them in `api_dashboard_config`.

Each route target keeps the existing card rules: `_refuse_locked` for edition locks, the risk order in `_SETTING_ORDER` for widen and tighten, Undo where the store keeps a value to restore.

### D3. Client-side rows applied in the client that clicks

A `localStorage` or desktop-app value cannot be written by the gateway, but the card's Apply button is always pressed in a dashboard client. The proposal: a `client` row's `setting.change` card writes the value in the clicking client, through the same `usePersisted*` hook or `useMainWindowSetting` bridge the control uses, and the card says it applies to this browser or this desktop app only. A card for a `client` row raised where no dashboard client can apply it (a messaging channel, a schedule) is refused with that reason.

### D4. `find_setting` names the next step for every row

`find_settings` keeps `writable` and adds, for a row that is not writable by `setting.change`:

- `kind`: the dedicated change kind to propose, for a `kind` row;
- `why`: the `READONLY_REASONS` code, for a `strict` row;
- nothing else for an `info` row, which is marked as such.

The `no_write_path` text that sends the agent to `settings.show` then only remains for rows still in the D1 baseline. The acceptance line of the issue, "no `writable: false` except where a documented security reason requires it", becomes: no row outside `strict` and `info`, and each `strict` row carries its documented code.

### D5. Remove where the page removes

- `denied_command` gains `action` `remove`, with the same confirmation as `toggle`. Removing a deny rule widens what the agent may run, so the card is proposed with risk `widen`.
- `find_setting` and the `change_kinds` guidance point to `request_server` with `action` `remove` for MCP servers, since `mcp.server` stays out of `list_change_kinds` by design (`GATEWAY_RESOLVED_KINDS`).
- String-list rows keep the existing `op` `add` and `remove` of `DASHBOARD_LIST_SETTINGS`; a new list row uses that shape.

### Phases

1. D1 class, extractor support, the test and the baseline. No behavior change.
2. D2 step 1 (the 11 dashboard keys) and D4.
3. D2 step 2 (Slack, Teams, Voice route targets) and D5.
4. D3 client-applied cards.
5. The channel tabs' 66 rows without a `configKey`, each channel through its own settings module, as their own pull requests.

Each phase removes its rows from the baseline in the same pull request.

## Safety

- The card stays the confirmation. No row is written without the person's click.
- `strict` rows do not move to `setting.change`. A dedicated kind for one of them (for example a safety switch with risk `widen`) needs its own maintainer decision.
- A route target calls the route the page calls, so every validation, overlay refusal and audit record of that route still applies.
- `client` cards never touch the gateway store, and they say so on the card.

## Alternatives considered

- **Let cards use `generic_spec`.** The Advanced page can already PATCH most scalar keys through `generic_spec`. Reusing it would cover many keyed rows at once, but `config_all_settings.py` says this is deliberately not done, and it would not cover the 258 rows without a `configKey`. Not proposed; listed as an open question.
- **A hand-kept list of covered rows.** That is the current state (`DASHBOARD_SETTINGS`); it does not fail when a row is added.
- **Drive the Settings page with the browser tool.** It breaks on a headless host, needs a live page and bypasses the card's preview and Undo.

## Open questions

1. Should `client` cards (D3) exist at all, or should those rows stay `info`-like with a guide link?
2. Do channel allow-lists (who may reach the agent, `WHY_REACH`-like) count as `strict` even where their channel page edits them freely?
3. May a card target a `generic_spec` key, or does the decision in `config_all_settings.py` stand?
4. Is the class prop on the panel source the right home, or should the class live in a Python table keyed by registry id?

## Measurement

All counts were taken on a detached worktree at `4172c08404`:

- Registry rows, writable rows and the `no_write_path` split: a script imported `kiro_crew.dashboard.change_cards_settings` from that tree's `src`, iterated `settings_registry()` and called `resolve_setting({"setting_id": id})` for each row, the same call `find_settings` uses to set `writable`. It reads no current values. Result: 357 rows, 57 resolved, 300 `no_write_path`, 258 of those without `configKey`.
- `_EDITABLE_CONFIG` size (144), `DASHBOARD_SETTINGS` (15), `DASHBOARD_LIST_SETTINGS` (1): `len()` of the imported tables.
- 24 kinds, 19 listed: `len(KINDS)` and `len(list_kinds())`.
- The 11 of 14 `dashboard.*` keys: a string search for each key name in `dashboard_config.py`. This shows the key is named there, not that every key is writable through `PUT`; each is to be confirmed when it is added.
- 19 `localStorage` call sites in 10 files: `git grep` for `usePersisted*(` and `localStorage.getItem`/`setItem` under `website/src/pages/settings`. This is a lower bound for client-side rows; the per-row split of the 258 rows without a `configKey` across client, route and info classes was sampled, not counted.
