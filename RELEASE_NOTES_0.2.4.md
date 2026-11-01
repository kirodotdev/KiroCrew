# Kiro BYO 0.2.4 — source-specific KiroCrew 0.11.0-rc.1 candidate

- BYO `0.2.4` metadata across App, Python, ACP and build outputs; legacy Crew
  `0.10` is no longer an install target.
- Reworked ACP integration for Crew 0.11's split runtime owners: ACP capabilities,
  MCP, sessions, routing, model selection, permissions, member session dispatch,
  Dashboard and session/load.
- Installer pins the exact original Python hashes for 20 changed Crew source
  files. Read-only ZIP audit checks 37 relevant upstream files and a total
  of 20 proposed semantic patches plus 1 new BYO Harness file.
- Isolated source-copy install, repeat install, uninstall and byte-for-byte
  restoration tested. The adapter refuses same-version but unknown source bytes.
- Crew 0.11 compiled dashboard is left unchanged rather than applying the
  unverified 0.10 HTML/JS patch. Compiled UI model filtering is not certified.
- Mate 2.0 Worker Kit is *not* certified for BYO; older global mate-linkage
  writes remain disabled. Tiga, Dyna, Crew explicit backend priority and
  previous BYO Agent Loop policies were not deliberately changed.

**Not yet verified:** installation on the user's live Windows Desktop Crew,
full application import with its installed dependencies, real Provider ACP
round trips, Mate Conductor/Worker Kit, schedules, and cross-platform E2E.
This is a contract-gated candidate, not a certified general release.
