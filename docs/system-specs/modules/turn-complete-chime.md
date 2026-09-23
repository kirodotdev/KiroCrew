# Conversation-Completion Chime

The dashboard requests a sound when a conversation hands control back to the user, or when an explicit question or approval needs an answer. A model turn ending while automated work remains does not request completion audio.

## Policy

`notificationEvent.ts:shouldChimeOnTurnDone()` accepts a slot-bearing event outside reconnect catch-up only when `continuing` is false or `needsInput` is true. It does not inspect the active slot, document focus, or tab visibility. Sound settings still decide whether an eligible event is audible.

`chat_utils.chat_done_payload()` stamps `chat_done` with `continuing` and `needs_input`. It is a coroutine, because it reuses `subagents_attached_async()` with the effective session key to cover running children, accepted-but-queued spawns, and results still being delivered. Active plan execution, pending synthesis, runnable queued prompts, active monitoring loops, and `WorkflowService.registry.has_pending_work_for()` keep the conversation open. The workflow query includes a terminal run whose driver is still flushing or handing off its result. A queue held after a sign-in failure is not runnable work. An unreadable activity probe suppresses completion audio without suppressing the terminal frame.

The deferred-compaction boundary explicitly marks itself continuing. The orchestrator's final exit can chime; a manual step-through pause explicitly reports `needs_input` because the next stage requires Go. These hints do not change scheduling, transcript finalization, or the running state.

## Wiring

- Chat runner, orchestrator, and stop handlers use the shared completion-payload builder. Remote relays retain their activity-hint-free frame and the client-side fallback because the local registry cannot certify a peer's pending work. `chat_done` still finalizes the transcript and refreshes the slot even when it requests no sound.
- `useWebSocket.ts` reads the event's complete continuation hint. A running workflow suppresses the parent's intermediate completion; its final synthesized reply can chime after the workflow ends. Work in another session, or a UI-launched workflow with no originating session, does not suppress this conversation.
- On an older frame with no continuation hint, the handler uses existing live child counts, queued-spawn counts, slot plan/queue fields, workflow selectors, and automation state. On a current frame, the server hint wins over those snapshots so a missed workflow terminal event or stale child/plan flag cannot silence a final reply.
- A live `question_card` requests the existing `approval` attention sound immediately, including while a blocking question parks the turn. Re-delivery of the same server card identity does not repeat the sound. A pending card suppresses a second completion chime when the agent ends its turn to wait for that answer. Reconnect rehydration is silent. Ordinary optional follow-up suggestions do not request attention audio.
- `approval` frames retain their attention sound independently of ongoing work.
- `dispatchMcNotification()` emits `MC_NOTIFICATION_EVENT` and contains listener failures. The frontend-only `turn` category remains unchanged so saved presets and Silent overrides keep working.
- `App.tsx` mounts `useNotificationSound()` application-wide. Disabled sound, zero volume, Silent presets, browser audio restrictions, and the existing burst cooldown still apply.
- `ThemeExperienceLayer.tsx` observes the same eligible notification event, so a theme's notification sound cannot bypass the conversation gate.
- `NotificationsPanel.tsx` retains the existing `turn` and `approval` rows and controls. Their copy states the gate rather than the old every-turn promise: the `turn` row is **Conversation handoffs** ("When a conversation finishes or pauses for your input") and the `approval` row is **Approvals and questions** ("When the agent needs a tool approval or an answer"), because a question card plays the approval sound. The stored `turn`/`approval` ids, the per-category selects, and the layout are unchanged; the settings registry re-exports the new English labels. No new setting is introduced.

The chime branch adds no notification-feed record or badge. Unread marks, slot refresh, voice-tail delivery, and ordinary feed notifications remain separate. App/cron/explicit agent notifications retain their own priority and channel-mute behavior.

## Opt-in background-completion toast

The native OS completion toast shares the conversation-attention gate because the OS can play a sound for it too. A pending question remains eligible for its named desktop toast even when its earlier question-card sound suppresses a duplicate completion chime; that toast requests `silent: true` so supporting platforms do not play a second OS sound. `chatCompleteNotify.ts:shouldNotifyOnChatComplete()` then applies its own default-off preference, granted notification permission, and away check (`document.hidden || !document.hasFocus()`).

The opt-in remains per-device under `mc-notify-chat-complete`; enabling it requests browser permission when needed. The title names the finishing slot and `kirocrew-chat-done:<slot>` coalesces by session. The body follows the same needs-input verdict the chime gate reads: a frame with `needs_input` or a live question card (an explicit question, a manual Go pause) says **Waiting for your input**; an ordinary completion, including a hint-free relay frame, keeps **Response ready**. Both bodies are catalog keys under `hooks.useWebSocket`. The constructor remains best-effort on platforms without page-context notifications. No new feed record is created.

## Tests and boundaries

`test_chat_completion_sound.py` exercises real queue-cycle completion and plan exits, including running/queued/delivering children, linked session identity, monitor state, queued recovery, held sign-in queues, and manual plan approval. `useWebSocket.conversationSound.test.ts` drives real reducers through WebSocket frames to cover workflow completion, live activity before slot snapshots, stale snapshots, question deduplication, reconnect replay, and cross-session isolation. `notificationEvent.test.ts`, `useNotificationSound.test.ts`, and `chatCompleteNotify.test.ts` pin the pure policy, saved audio settings, and native-toast gate, including which body each completion kind carries. `NotificationsPanel.test.tsx` pins the two category rows' copy and `notificationAttentionCopy.test.ts` pins that every shipped catalog keeps the two toast bodies and the two rows distinguishable.

Remote relays describe work visible to the local gateway and any relayed child/workflow events; a disconnected peer's unreported work is not inferred. This change does not introduce a new persisted conversation state or alter monitor terminal-notification delivery.
