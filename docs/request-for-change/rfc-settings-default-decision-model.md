---
title: Let the Default Model on Settings, Chat, Model be Auto (decision model)
status: draft
author: kirocrew agent session, directed by jeeshofone
created: 2026-11-16
last-audited: 2026-11-16
audited-at: c2f1b329a3
doc-pr:
implementation-prs: []
tracking-issues: [31302]
supersedes: []
superseded-by: []
---

# RFC: Let the Default Model on Settings, Chat, Model be Auto (decision model)

Status: draft. Part of [#31302](https://github.com/kirodotdev/KiroCrew/issues/31302), which is parked as design work (`tier:T3`); acceptance is requested from a maintainer of the decision seam and the chat settings. Nothing is built on main. A working prototype exists on branch `feat/settings-default-decision-model` of the `jeeshofone` fork at `3920ec45a3`, with tests and screenshots, so a reviewer can try the shape before deciding; it is not a pull request. Code references were read on main at `c2f1b329a3`.

## Summary

The chat model picker offers **Auto (decision model)**: each message's model is picked by the `model.route` decision point. The **Default Model** select on Settings > Chat > Model does not offer it, so an owner who wants routing has to pick the entry again in every new chat. This request for change (RFC) proposes that the Default Model select offer the same entry under the same condition, and that choosing it start each new chat in exactly the state the chat picker leaves.

The choice is stored as `agent.model: "auto"` plus a new owner-only boolean, `agent.model_jev_route`, written by the same configuration PATCH request that saves `agent.model`. New chats then start on `auto` with the existing per-chat `jev_route` flag set, but only where the decision gate would route them. Anything else starts them on plain inherit, as today.

## Motivation

### How decisions and model routing work today

The decision seam (`src/kiro_crew/decisions/`) asks a small model a closed question at a fixed point and falls back to the built-in behaviour on any refusal. The points are listed in `DECISION_POINT_NAMES` (`src/kiro_crew/decisions/gate.py`).

- **Deciders.** The shipped provider is hosted Jev from TypeSafe (`decisions.provider.endpoint` defaults to `https://api.typesafe.ai/v1/systemone`, model `jev-latest`, `src/kiro_crew/config/sections_decisions.py`). The same client also talks to Cloudflare Clef on Workers AI (`src/kiro_crew/decisions/impl_systemone.py`), to a System One model hosted on an Amazon SageMaker endpoint (a `sagemaker://` address), and to three local presets the gateway can run itself: Plumb-4B, Strands Decider 2B and Laya (`LOCAL_MODELS` in `src/kiro_crew/decisions/local_models.py`). "No decision model" (`ENDPOINT_NONE`) sends nothing.
- **Consent.** There is no `enabled` field in `config.json`, because that file is writable by an agent. The switch is the keystone file `decisions_consent.json`, written only by the owner through `PUT /api/decisions/consent`. That route reports two fields: `enabled` is the switch, and `permits` is the effective answer, which also requires the consent to be for the endpoint config names now and folds in any governance denial (`_payload` in `src/kiro_crew/dashboard/handlers/decisions.py`). The gate's own read is `_consented_for` (`src/kiro_crew/decisions/gate.py`).
- **Governance ceiling.** `capabilities.decisions`, the narrower `capabilities.decisions_local` and a per-model `providers` allow list (`DECISIONS_SCOPE`, `DECISIONS_LOCAL_SCOPE` and `PROVIDERS_LIST` in `src/kiro_crew/decisions/capability.py`). The dashboard reads the fleet answer as `decisions_enabled` on `GET /api/dashboard/config` (`src/kiro_crew/dashboard/file_api/dashboard_config.py`).
- **`model.route`.** The point puts one turn in a `simple`, `medium` or `complex` tier and maps the tier to a model through `decisions.model_route` (`DecisionsConfig.model_route` in `src/kiro_crew/config/sections_decisions.py`). Every tier is empty by default, so nothing moves until the owner pins tiers on the Decisions card (the `model.route` tier selects in `website/src/pages/settings/DecisionsPointPanel.tsx`). Only a turn whose actor is the user is routed (the `_jev_route_armed` call in `src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py`); cron deliveries, sub-agent turns, crew relays, app injections and autonudge wakes are excluded, because each already resolves its model through its own tier. `decisions.model_route_derive` separately routes the start of an unpinned sub-agent.

### How the per-chat entry works today

1. The picker draws the row only when `jevRouteOffered` holds: `decisions_enabled === true`, consent `permits === true`, and a live slot (`website/src/lib/jevRoute.ts`). The row's id is `JEV_ROUTE_MODEL = "auto:jev"` (in `website/src/lib/jevRoute.ts` and `src/kiro_crew/dashboard/chat_handlers.py`).
2. `switch_slot_model` (`src/kiro_crew/dashboard/chat_api/slot_model.py`) refuses the pick from anyone but the owner (operation `chat_slot_model_jev_route`), stores `slot.model = "auto"` and sets `slot.jev_route`. The sentinel never reaches `slot.model`, so `slot.model` stays a provider model id (the `jev_route` comment in `src/kiro_crew/dashboard/state_parts/slot_session.py`).
3. `_jev_route_armed` (`src/kiro_crew/dashboard/chat_turn/decisions_hooks.py`) routes a slot whose `jev_route` is set or whose model is `auto` (`_JEV_ROUTE_AUTO_MODELS`), and `_jev_preview_on` asks `gate.is_enabled` off the event loop. A chat that names no model (`""`) is not routed.
4. `jev_route` is never written to or read from transcript metadata (`_read_model` in `src/kiro_crew/dashboard/slot_persistence/metadata_codec.py`), because that file is agent-writable. After a restart a picked chat keeps routing through its `auto` model.
5. Any manual pick in the chat clears the flag.

### The gap

A new chat is created with `slot.model = ""`, which inherits the Default Model and is not routed. Saving plain `auto` as the Default Model does not route new chats either, because they still store `""`. The Default Model select (the `models` page of `website/src/pages/settings/ChatPanel.tsx`, saving `agent.model` through `PATCH /api/config/kirocrew`) lists only the advertised models and Auto. So the only way to route a new chat is to pick the entry in it, every time. That is the request in #31302.

## Goals

- The Default Model select offers Auto (decision model) under exactly the chat picker's condition, from the same helper.
- Choosing it starts each new owner chat in the state the chat picker leaves: `slot.model = "auto"` and `slot.jev_route = true`.
- It fails closed: wherever the gate would refuse, or an agent pins its own model, the new chat starts on plain inherit.
- The page says why the entry is not offered, rather than hiding it silently.

## Non-goals

- The Background and Subagent model selects. `model.route` never routes those turns, so the entry would do nothing there.
- A second editor for the tier models. They stay on the Decisions card; this page links to it.
- Persisting `jev_route` in transcript metadata, or changing how an existing chat routes.
- Any change to the per-chat picker.

## 1. Design

### 1.1 Storage

The `agent.model` PATCH (`src/kiro_crew/dashboard/handlers/core_config_patch.py`, already owner-only through `require_owner_dashboard_request`) resolves the entry the way `switch_slot_model` does for a chat. A value of `auto:jev` is saved as `agent.model: "auto"` and records `agent.model_jev_route: true`. Any other `agent.model` save removes the flag. The flag has no PATCH path of its own, so there is one way to write it, and it is owner-only because the PATCH is.

A `config.local.json` overlay that pins either `agent.model` or `agent.model_jev_route` refuses the save with the existing `config_overlay_owned` answer, so the page never reports a choice the overlay would undo.

### 1.2 The Default Model row

`useChatPanelModels.ts` adds the row with `jevRouteOffered` and `withJevRoute` from `website/src/lib/jevRoute.ts`. `hasSlot` is passed as true, because the gateway resolves the id on save, so there is no pending model that could carry the sentinel into a slot. The select shows the entry when `agent.model_jev_route` is true and `agent.model` still defers (`auto` or empty).

A line under the select explains the entry:

- Offered and chosen: new chats let the decision model pick each message's model, with a link to the Decisions card for the tier models.
- Not offered because `decisions_enabled` is false: the organisation does not allow decision models.
- Not offered because consent does not permit: the entry appears once the Decisions switch is on for the address it sends to, with the same link.
- A saved entry the gate now refuses is still shown, with a note that new chats start on Auto instead.
- A failed consent read draws nothing and offers nothing, as the chat picker does.

### 1.3 New chats

`api_chat_slot_create` (`src/kiro_crew/dashboard/chat_api/slot_create.py`) sets `slot.model = "auto"` and `slot.jev_route = true` on a new owner chat when all of these hold:

- `agent.model_jev_route` is true and `agent.model` still defers;
- the create names no model, and is not ephemeral or an app page's chat;
- it is not a Bot's own chat, which has no picker to undo it;
- no agent the chat runs pins a model of its own, checked with the existing tier-1 and tier-2 lookup behind `resolve_effective_model` (`_agent_tier_model` in `src/kiro_crew/config/loader.py`). The Crew Mode conductor counts, because the client switches a new chat onto it right after the create;
- `gate.is_enabled("model.route")` holds for the chat's session, read off the loop through `_jev_preview_on`.

The arm is applied inside the create's slot-push suspension, before any save or broadcast describes the chat, and the slot is marked dirty so the periodic flush writes `model` into a first metadata line the agent selection may already have written. Anything else starts the chat exactly as today. From then on the chat is indistinguishable from one where the owner picked the entry: a manual pick clears the flag, and a restart keeps it routing through its `auto` model.

### 1.4 Consent withdrawn later

Nothing is rewritten. New chats start on plain inherit while the gate refuses, and the routing already armed on open chats routes nothing, because the turn-time gate refuses as it does for a per-chat pick. The row explains the state. Choosing a concrete Default Model clears the flag.

## 2. Alternatives considered

- **Store the sentinel in `agent.model`.** Rejected. Every reader of `agent.model` (`resolve_effective_model`, the provider factory, workers and crons) would need a filter, which is the breakage the `jev_route` comment in `slot_session.py` was written to avoid for `slot.model`. The prototype keeps `auto:jev` out of every stored field.
- **A separate toggle, such as "Start new chats on Auto (decision model)".** Rejected. It would say the same thing as a Default Model value in a second control, and the two could disagree (a concrete Default Model with the toggle on). One select with one row matches the chat picker.
- **Route every chat that inherits the default at turn time.** Rejected. It would also route channel chats and other slots that never had a picker, and it would change the meaning of an empty `slot.model`.
- **Put the control on the Decisions card.** Possible; see open question 1.

## 3. Migration plan

One phase, one pull request, the prototype branch.

Exit criteria:

- An owner save of the entry stores `agent.model: "auto"` and `agent.model_jev_route: true`; a non-owner save is refused and writes nothing; any other save drops the flag.
- An overlay pinning the flag refuses the save with `config_overlay_owned`.
- A new owner chat starts on `auto` with `jev_route` when the gate permits, and on plain inherit when it refuses or when its agent pins a model.
- The row is drawn only when both `decisions_enabled` and `permits` are true; each unavailable state explains itself; a failed consent read claims nothing.

The prototype carries one backend test file (4 tests) and one frontend test file (6 tests) for these, and all 10 fail against main's production code.

## 4. Backward compatibility

Compatible. `agent.model_jev_route` is a new optional boolean that reads false when absent. `auto:jev` is refused by today's model-pin grammar, so no stored value changes meaning. Reverting removes the behaviour and leaves an ignored key.

## 5. Security considerations

The flag is written only through the owner-only PATCH, and routing still spends only against the owner's keystone consent and the owner's tier map, both re-checked on every turn. A prompt-injected agent that edits `config.json` to set the flag gains no more than it could already get by setting a chat's `model` to `auto`, which the turn gate also routes while consent stands.

## 6. Open questions for maintainers

1. **Placement.** Does the row belong in the Default Model select (where #31302 asks for it), or as a switch on the Decisions card beside the tier models?
2. **Crew Mode.** Should a pinned default agent block routing when Crew Mode will switch the chat to an unpinned conductor? The prototype refuses in that case, because the server cannot know whether the client's switch will land.
3. **App page chats.** Should a chat opened from an app page also start routed? The prototype skips them, because their first line is written before the arm and the app's own model pin is their tier.
4. **Other writers.** Should `kirocrew config set agent.model` and the settings change cards also clear the flag? The prototype clears it only from the Settings save; the flag takes effect only while `agent.model` defers.
