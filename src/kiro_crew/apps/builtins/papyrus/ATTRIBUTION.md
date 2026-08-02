# Attribution

Papyrus was written by **tricatte** as a standalone app named `Papyrus` for a
KiroCrew-family dashboard, and is included here as a first-party builtin app with
the original author's name preserved in `app.json`.

## What was kept

- The product: a split-pane LaTeX editor with a live PDF preview and an AI
  co-author panel, project management, and git clone/commit/pull/push.
- The compilation behaviour: compiler discovery (`pdflatex`, then `tectonic`,
  then a userspace TeX Live install), the `BSTINPUTS`/`BIBINPUTS` search-path
  extension that lets conference templates find their `.bst` files, the
  `pdflatex -> bibtex -> pdflatex -> pdflatex` bibliography cycle, and the
  "Rerun to get..." retry.
- The compiler-log parser and its hard-won details, notably that consecutive
  `! error` lines must not borrow one another's `l.<n>` line reference.
- The path-containment rules for project-relative file paths, and the check that
  a `.papyrus.json` arriving inside a cloned repository cannot name a main
  document outside the project.
- The git pull autostash flow, including keeping the stash when the pop
  conflicts rather than discarding the user's work.

## What changed for this repository

- The backend was rewritten from a standalone `ThreadingHTTPServer` on its own
  port into in-gateway aiohttp routes under `/api/apps/papyrus/*`, with every
  subprocess and filesystem call moved off the asyncio event loop.
- The frontend was rewritten against this repository's own conventions: Monaco
  instead of CodeMirror, the browser's native PDF viewer instead of a bundled
  `pdf.js`, React Query for server state, Lucide icons, the shared page-layout
  components, and translated strings for every locale the dashboard ships.
- Compilation now passes `-no-shell-escape` explicitly and runs through the
  gateway's sandbox spawn chokepoint with a kernel resource ceiling.
- The `papyrus-writing` skill ships under `src/kiro_crew/builtin_skills/` so
  every install receives it.
- Compiler discovery gained a fourth, last-resort location: the app's own
  **managed Tectonic install** (`backend/tectonic.py`), which a user provisions
  in one click from the Papyrus page. Upstream, a host with no TeX simply could
  not compile. The managed copy is probed after PATH and the userspace TeX Live
  locations, so it never displaces a real distribution. See the "Managed
  compiler" section of `docs/system-specs/modules/papyrus.md`.
