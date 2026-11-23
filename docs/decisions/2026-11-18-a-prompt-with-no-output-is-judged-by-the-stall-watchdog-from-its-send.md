# A prompt that gets no output is judged by the stall watchdog from the moment it is sent

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-18

## Decision
The session handle's model-wait watchdog judges a prompt from its send, not only after the
model's first text. Before the turn's first frame, a reading of no backend connection and
flat counters (`DEAD`) gets the ordinary stale window instead of the immediate probe; a
held connection or macOS gets the model-silent window, and a visibly busy agent the 2-hour
cap. A probed turn that streamed nothing, not even a native sub-agent's activity, is sent
again verbatim on the dashboard chat and in a sub-agent run. Once anything has streamed,
recovery is unchanged.

## Why
- No watchdog judged a prompt that got no frame at all, so only the 4-hour turn ceiling or
  the backend's own error ended it: a message waited 60 minutes with no output before the
  backend failed it.
- It reuses the existing watchdog and its windows (10 minutes, 30 minutes while the backend
  connection is held or on macOS, at most the 2-hour cap) instead of adding a new deadline.
- A cancelled prompt the agent never answered is not kept by it, so a continue-nudge would
  have nothing to continue; the message itself is what has to be sent again.

## Costs accepted
- A silent first think that runs past its window is cancelled and sent again.
- A busy co-tenant on a shared runtime reads as busy (`WORKING`), so a silent prompt there
  is bounded by the 2-hour cap.
- Channels, the native Slack turn, the CLI chat and background turns end such a turn at its
  window with no re-send, where before they waited for the ceiling or the backend's own
  error.
- Each dashboard re-send fires `UserPromptSubmit` again.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/38366#issuecomment-6966646710 -- the
  maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/38366 -- the report.
