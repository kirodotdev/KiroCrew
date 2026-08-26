# Feature specs

Specs for user-visible features that span several modules. A feature owned by a
single subsystem belongs in [../modules/](../modules/README.md) instead.

| Spec | Covers |
|---|---|
| [dashboard-token-auth.md](dashboard-token-auth.md) | Signed, IP-pinned dashboard tokens, session TTLs, and token refresh. |
| [session-work-ledger.md](session-work-ledger.md) | Per-session durable work state (goal, phase, tried, artifacts) on disk, its MCP tools, and monitor-loop snapshot injection. |
| [babysit-pr-watch.md](babysit-pr-watch.md) | Zero-token PR polling for babysit loops: a script cron that wakes the owning session only on unexpected state. |
| [agent-interrupt-controller.md](agent-interrupt-controller.md) | `kiro_crew.irq`: masking, coalescing, epoch resets and an error backstop for script-cron pollers, so a cheap probe interrupts an expensive agent turn instead of the turn polling. Also the app-facing probe SDK. |
| [prompt-optimizer.md](prompt-optimizer.md) | Rewriting a draft prompt on demand, and the paste-forwarding surface. |
| [app-notifications.md](app-notifications.md) | How an app publishes a notification to the local bus. |
| [inline-action-buttons.md](inline-action-buttons.md) | Agent-proposed buttons rendered inline in chat. |
| [workflow-chat-cards.md](workflow-chat-cards.md) | Rendering a workflow run's progress as a chat card. |
| [steering-viewer.md](steering-viewer.md) | Viewing the steering files a session loaded. |
| [stt-streaming.md](stt-streaming.md) | Live speech-to-text partials in the composer. |
| [voice-streaming.md](voice-streaming.md) | Streaming voice replies, and the text normalization applied before synthesis. |
| [turn-complete-chime.md](turn-complete-chime.md) | The end-of-turn audio cue. |
| [turn-stats-footer.md](turn-stats-footer.md) | The per-turn token and timing footer. |
| [model-fallback.md](model-fallback.md) | The throttle-exhaustion model fallback (`agent.fallback_model`): trigger, shared walk, sticky restore, visibility. |
| [code-approvers.md](code-approvers.md) | Tier routing for code review approvers. |
| [claude-code-provider.md](claude-code-provider.md) | The removed standalone provider, kept as the record of what the KiroACP-only collapse took out. |
