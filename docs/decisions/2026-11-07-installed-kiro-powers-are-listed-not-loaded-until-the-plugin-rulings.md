# Installed Kiro Powers are listed, not loaded, until the Agent Plugins rulings are made

Decided by: Bolin Chen (Kiro Crew maintainer, @bolichen97)
Date: 2026-11-07

## Decision

Kiro Crew lands the Agent Plugins loader in phases: discovery and validation (Phase 1), the converter (Phase 1b) and an owner-only, read-only list of every installed Power (`GET /api/agent-plugins` and the Customize > Kiro Powers tab, where a `POWER.md` Power reads as not loaded with its reason) ship now and load nothing; the on/off switch, the launcher, the plugin MCP source, any `includePowers` pin and install wait for the rulings below; the shipped forwarding of a spec-stated `includePowers` keeps working unchanged; Kiro Crew adds no `POWER.md` loader or converter, and instead asks the `kirodotdev/powers` owners by 2026-11-14 to republish the catalog's `POWER.md` Powers as Agent Plugins (`plugin.json`), with Kiro Crew maintainers sending those conversion pull requests themselves if no committed date comes by 2026-12-01.

## Why

- The loader's case is support for the Agent Plugins standard and for Claude Code, Codex and Cursor plugins converted at install (#27710), not #9212. It accepts 3 of the 31 catalog Powers; the other 28 are `POWER.md` Powers. So #9212 stays mostly unfixed until the catalog moves, and republishing fixes it at the source, for Kiro IDE and every other client too, without a translator in Kiro Crew.
- Phase 1 and 1b add new files only. They change no core file and no agent spec, and nothing activates a plugin, so they can ship before the rulings. The read-only list gives people the answer "listed, not loaded, and why" now.
- The rest of the stack (#28808, #28809, #28827) reverses a released feature (`includePowers` reaches a KAS session since 0.8.0, #13015), writes a new key into every spec Crew writes with no kiro-cli version floor, leaves user-written specs forwarding `true`, and makes Crew a second writer of Kiro IDE's `installed.json`. Those are rulings, not review findings.
- Before Phase 2 can land, each of these is ruled on in its own entry: (1) open question 4 of `rfc-agent-plugins-load-installed.md`, after a probe of whether the bundled KAS loads a `POWER.md` folder when `includePowers` is true: if it does not, a superseding entry replaces the 0.8.0 contract by dropping a stated `includePowers` at the one KAS projection (`to_client_custom_agent` in `acp/kas_agents.py`) with a session notice, instead of writing `false` in seven spec writers; if it does, forwarding stays until the catalog moves; (2) the edit risk, consent to a plugin's name (#28808) or to a digest of its content (the RFC as landed), signed by the security owner; (3) the lowest kiro-cli release that accepts any spec key Crew writes for plugins; (4) the second writer of `installed.json`.
- Rejected: keeping today's state with a troubleshooting entry only (users keep two configs that drift, and the written stack is discarded); landing the whole stack as written (the reasons above); converting `POWER.md` Powers at install (a reverse-engineered translator for a format Kiro is leaving, built only if the catalog refuses the conversion pull requests); reading `POWER.md` in the loader (two formats in the load path for good, against the RFC's strict-client non-goal); and an opt-in KAS native loader (KAS only, against harness parity, with no per-Power switch and no Crew consent).
- The standard RFC was accepted by its own author, so Ray Xu (project owner, @buluoray) is asked to co-sign this entry on the pull request that adds it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/9212 -- the request: installed Kiro Powers never reach Kiro Crew sessions.
- https://github.com/kirodotdev/KiroCrew/issues/27710 -- Agent Plugins standard support on every harness.
- https://github.com/kirodotdev/KiroCrew/pull/28461 -- Phase 1, discovery and validation, rebased onto main here.
- https://github.com/kirodotdev/KiroCrew/pull/28686 -- Phase 1b, the converter, rebased onto main here.
- https://github.com/kirodotdev/KiroCrew/pull/28808 -- Phase 2 as written, the source of the read-only list's `GET` half; its switch, launcher and `includePowers` pin are held.
- https://github.com/kirodotdev/KiroCrew/pull/13015 -- the shipped `includePowers` forwarding this entry keeps.
- https://github.com/kirodotdev/KiroCrew/pull/33621#issuecomment-6779597961 -- the maintainer (@bolichen97) confirming this phased decision and keeping the converter in Phase 1; Ray Xu (@buluoray) is asked to co-sign on this pull request.
