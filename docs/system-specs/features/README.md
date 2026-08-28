# Feature specs

Specs for user-visible features that span several modules. A feature owned by a
single subsystem belongs in [../modules/](../modules/README.md) instead.

| Spec | Covers |
|---|---|
| [aws-control.md](aws-control.md) | The AWS account portal and S3-backed cloud drive app: accounts, Drive/Library/Backup, consent and confirmation guards, sharing. |
| [dashboard-token-auth.md](dashboard-token-auth.md) | Signed, IP-pinned dashboard tokens, session TTLs, and token refresh. |
| [session-work-ledger.md](session-work-ledger.md) | Per-session durable work state (goal, phase, tried, artifacts) on disk, its MCP tools, and monitor-loop snapshot injection. |
| [babysit-pr-watch.md](babysit-pr-watch.md) | Zero-token PR polling for babysit loops: a script cron that wakes the owning session only on unexpected state. |
| [agent-interrupt-controller.md](agent-interrupt-controller.md) | `kiro_crew.irq`: masking, coalescing, epoch resets and an error backstop for script-cron pollers, so a cheap probe interrupts an expensive agent turn instead of the turn polling. Also the app-facing probe SDK. |
| [mcp-probe-quarantine.md](mcp-probe-quarantine.md) | A durable consecutive-probe-failure count per MCP server, surfaced on its dashboard row with a reset control. The unmount half is deferred; the spec records why. |
| [prompt-optimizer.md](prompt-optimizer.md) | Rewriting a draft prompt on demand, and the paste-forwarding surface. || [app-notifications.md](app-notifications.md) | How an app publishes a notification to the local bus. |
| [inline-action-buttons.md](inline-action-buttons.md) | Agent-proposed buttons rendered inline in chat. |
| [workflow-chat-cards.md](workflow-chat-cards.md) | Rendering a workflow run's progress as a chat card. |
| [steering-viewer.md](steering-viewer.md) | Viewing the steering files a session loaded. |
| [stt-streaming.md](stt-streaming.md) | Live dictation in the composer: the three providers, the WebSocket frames, the local recognizer's endpointing and partial pipeline, and the model download. |
| [voice-streaming.md](voice-streaming.md) | Streaming voice replies, and the text normalization applied before synthesis. |
| [turn-complete-chime.md](turn-complete-chime.md) | The end-of-turn audio cue. |
| [turn-stats-footer.md](turn-stats-footer.md) | The per-turn token and timing footer. |
| [model-fallback.md](model-fallback.md) | The throttle-exhaustion model fallback (`agent.fallback_model`): trigger, shared walk, sticky restore, visibility. |
| [code-approvers.md](code-approvers.md) | Tier routing for code review approvers. |
| [claude-code-provider.md](claude-code-provider.md) | The removed standalone provider, kept as the record of what the KiroACP-only collapse took out. |
