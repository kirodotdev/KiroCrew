# Kiro BYO 0.2.5 · Crew 0.11.0-rc.1 compatibility candidate

- Preserve exact Crew 0.11.0-rc.1 source-hash compatibility gate, Crew-native permission authority, Mate 2.0 safety limit, and front-end Tiga labels.
- Add bounded `update_plan` native tool: 20 session-scoped steps; `replace`/`get` operations; strict schemas, duplicate-ID rejection and one active step. This is **not** a second scheduler; plans are deliberately volatile and lost on BYO restart.
- Improve read-only tool-center diagnostics with actionable rejection causes and historical-snapshot warnings. It does **not** grant/revoke permissions, change Crew trust mode, or advertise unverified live MCP permissions.
- Browser/visual tasks continue through Crew-granted MCP tools only; this release does **not** add unrestricted browser automation or new OS privileges.
- Target is the user's uploaded Crew 0.11.0-rc.1 source. New Crew release builds need their own source-contract validation. No Windows/macOS/Linux desktop E2E is claimed.

## Crew 0.11 RC1 UI streamlining candidate (same product version, new build fingerprint)

- Removed the standalone BYO **Tool Center** tab, screen, and UI polling/request code. KiroCrew's chat trust/permission dialog remains the **only** user-facing authorization flow. This does **not** bypass MCP attachment, live verification, agent tool scope, or Crew authorization decisions.
- Removed the now-unused `toolcenter.*` frontend translations across all five locales. Kept runtime MCP diagnostic recording and the token-protected standalone `/api/tool-center` endpoint for developer diagnostics; there is no duplicate Crew App permissions screen.
- Found a concrete frontend wiring error in the previous release: the app page called `/api/apps/kiro-byo/tool-center` although `backend/routes.py` never registered that route. The separate local management service's `/api/tool-center` is **not** the same endpoint. The misleading empty tool list is eliminated with the screen.
- Audited the other eight BYO pages for render entrypoints, lazy loading, locale coverage and Crew App-route parity; validated the 0.11 RC1 frontend source seams for model dropdown and settings labels. The existing chat Type display and Crew bottom status bar were not changed.
- Crew 0.11 RC1 exact-source checks remain fail-closed. Windows desktop visuals, Mate 2.0 full Worker Kit and real `learn_add` execution still require real-world validation.
