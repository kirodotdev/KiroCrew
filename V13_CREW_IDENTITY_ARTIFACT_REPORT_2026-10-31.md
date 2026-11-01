# Kiro BYO 0.3.0 V13 — Crew Identity and Native Artifacts

## Scope

V12-source baseline. This is a limited host-affordance fix, not a new artifact service or a redesign of KiroCrew's UI.

### Verified source behavior

- Each routed provider request receives a **fresh**, short KiroCrew host identity block: host KiroCrew, execution backend Kiro BYO, Crew agent ID if supplied, active Mode/Type if the Type policy really routed the turn, and an explicit distinction when a concrete model bypasses adaptive Type routing. The saved Crew Agent prompt still appears first and is not replaced.
- On visualization/widget requests in simplified Chinese, Traditional Chinese (shared core terms), English, Japanese or Korean, the model receives a clear hint to use Crew's inline `<mcwidget>` format when an in-chat widget is appropriate, and the verified first-party `skill_search` when available. The model is told not to fabricate scores or to claim persistence before host confirmation.
- Explicit artifact management requests expose `artifact_save`, `artifact_get`, `artifact_update`, `artifact_list` and `artifact_versions` **only** if the Crew-owned `kirocrew-core` MCP is live/verified and the saved Agent allows each binding. `artifact_delete` / `artifact_revert` are not preloaded. Underlying permission and tool execution checks are unchanged.
- Any other tool still requires normal deferred search and host permissions. Unverified / third-party lookalike tool names are not promoted.
- No local artifact database, no backdoor tool execution, no automatic creation or deletion of user files.

### Implementation

- `src/kiro_byo/crew_turn_context.py` — pure host context and five-language intent hints; separate artifact tool hint used only by a current turn.
- `src/kiro_byo/turn_runner.py` — composes factual context after route selection and exposes only permitted first-party tools where relevant.
- `src/kiro_byo/mcp_client.py` — verified first-party non-destructive artifact tool discovery.
- `tests/test_crew_identity_artifact_context.py` — new deterministic tests.
- `tests/test_acp_control_plane.py` — integrated provider-facing prompt tests.

### Limitations / non-goals

- A system hint makes the preferred workflow discoverable; **model compliance is probabilistic**, not a guarantee it will call the Skill. The host still owns tool availability and authorization.
- We do not claim Widget auto-registration, artifact save/update, cross-session reuse, or rollback has been validated on the user's live Windows install; that requires actual Crew host MCP/ACP and UI testing, ideally also on macOS and Linux.
- Inline Widget rendering is provided by KiroCrew, not a new BYO API. Arbitrary HTML workspace files do not automatically become a Crew artifact.
- The user-requested Crew Agent identity takes precedence over any upstream model persona. Do not infer a false active Type for concrete direct-model sessions.

### Manual end-to-end acceptance checklist

1. Switch Crew official backend -> BYO Dyna Flash, new chat asks 'who are you and what Mode/Type?'. It should say Crew assistant / Kiro BYO backend / Dyna Flash (not the upstream model's product).
2. BYO Tiga Multi -> Dyna Flash -> a pinned **concrete** model. Recheck identity each turn, especially the concrete bypass claim. Switch back to official and confirm BYO context is absent.
3. Ask in Chinese to draw an interactive comparison directly in chat as a Crew artifact. Verify `skill_search` discovery, inline widget rendering, artifact-library auto-registration, and no unrelated filesystem HTML generated unless explicitly requested.
4. Save, read, modify, version, and use the artifact from a second chat. Check permissions; unpinned artifacts' lifecycle is Crew-owned.
5. Restrict Saved Agent's MCP permissions and verify artifact tools disappear and cannot execute. Attempt a deletion request and ensure normal approval/explicit selection, never automatic access.
6. Repeat on Windows, macOS, Linux and the supported Crew release contracts. Do not infer E2E parity from Python unit tests.
