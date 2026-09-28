# Apps and the App Store

An **app** is a feature that ships as its own unit: it brings its own pages in the
dashboard, and it may also bring agents, skills, MCP tools, scheduled jobs and a
backend process. Twenty-four apps ship inside the Kiro Crew package — Meetings,
Papyrus, Mochi, Dev Fleet, Notes and the rest — and almost all of them are **off
until you turn them on**. The App Store is where you turn them on, and where you
install apps that did not ship with your build.

If you just want to look around: **sidebar → Apps**. Everything shipped is already
on disk, so a shipped app needs **Enable**, not Install. An app with a page of its
own takes a sidebar entry the moment it is enabled; Command Bar has no page and
replaces the quick-search overlay instead.

## The App Store

Two main pages, one set of data:

| Page | Route | What it is for |
|---|---|---|
| **Discover** | `/apps` | The storefront: featured collections, a category rail, and every app the catalog offers. Install from here. |
| **Library** | `/apps/library` | Your installed apps as an icon grid, with each tile offering the actions that apply to that app in its current state. |
| Detail | `/apps/detail/:name` | One app's description, screenshots, highlights, publisher and version. |
| Updates | `/apps/-/updates` | Every pending update in one worklist, with **Update All**. |
| Migration | `/apps/migrate/:name` | Guidance for an app that used to ship inside the package and no longer does. |

Discover's shelf comes from the published catalog at `apps.crew.kiro.dev`, cached for
an hour. When that fetch fails the store falls back to the small seed list bundled in
your install, so the page still renders — but it is a short shelf, and an app the
catalog alone carries is not on it until the fetch succeeds again. Library is
unaffected: it reads your own machine, so everything installed is still listed.
**Sources** (the gear in the Discover header) is where you add an organization's own
registry, or install a local directory from a path.

### Enable, disable, update, uninstall

- **Enable** is the activation gate. A disabled app contributes no pages, no agents,
  no skills, no crons and no backend process.
- **Disable** reverses all of that. Its data directory is kept.
- **Uninstall** applies to apps you installed; a shipped app cannot be uninstalled,
  only disabled. Uninstalling keeps the app's data by default. Pass `--purge-data` on
  the CLI to delete that too — which is not reversible.
- The pin badge on a Library tile controls whether an enabled app shows in the
  sidebar. It does not disable anything.

Two shipped apps are on from a fresh install: **Command Bar** and **Task Runner**.
Everything else waits for you.

## What ships

Each line below is drawn from the app's own manifest description.

### Build and ship software

| App | What it does |
|---|---|
| **Dev Fleet** (`dev-fleet`) | A control panel for working on Kiro Crew itself: every feature worktree with its pull-request state, and any of them brought up as a preview pod. |
| **Code Review Sage** (`code-review-sage`) | Deep-reviews GitHub pull requests and tells you which changes actually deserve your attention. |
| **Auto-Improvement** (`auto-improvement`) | Measures a GitHub repository before it changes it, then runs keep-or-revert improvement cycles against that metric. |
| **Issue Radar** (`issue-radar`) | An issue triage assistant that remembers — GitHub, GitLab and Azure DevOps items with suggested labels and a local findings ledger. |
| **Spec Builder** (`spec-builder`) | Spec-driven development in-app: a feature idea becomes Requirements → Design → Tasks, then an execution session. |
| **Design Tweak** (`design-tweak`) | Select elements in your live app preview and turn them into scoped, source-mapped edit requests for the agent. |
| **Design Critique** (`design-critique`) | A design critique you can run before you ask a colleague, from a screenshot, a flow, a Figma file, a repo or a live URL. |
| **Create Folders From Project** (`project-scaffolder`) | Reads a project directory, works out which sub-projects live inside, and makes a matching sidebar folder for each. |

### Write, present and record

| App | What it does |
|---|---|
| **Papyrus** (`papyrus`) | A LaTeX paper editor with live PDF preview and an AI co-author. |
| **PPTX Maker** (`pptx-maker`) | Describe the deck you want in chat and get a real `.pptx` back. |
| **Notes** (`md-notebook`) | A markdown notebook that lives in a git repository, so your notes are versioned and readable by any other editor. |
| **Meetings** (`meetings`) | An AI meeting assistant: live transcription, structured notes, and action items you review before filing. |

### Run work on its own

| App | What it does |
|---|---|
| **Task Runner** (`projects`) | Hand over a multi-step job and let it run to completion unattended. See [task-runner.md](task-runner.md). |
| **Workflows** (`workflows`) | Author, run and watch multi-phase agent runs written from a plain-language goal. See [workflows.md](workflows.md). |
| **Research Lab** (`auto-research`) | Multi-cycle research campaigns that keep working after you walk away. See [research-lab.md](research-lab.md). |
| **Channels** (`channels`) | A shared room where several agents work one problem in the open, with no private agent-to-agent messaging. |
| **Ops Mission Control** (`ops-mission-control`) | An autonomous ops first responder: claims what fires, investigates it, proposes a fix, and keeps what it learns. |

### Your machine and your cloud

| App | What it does |
|---|---|
| **Files** (`file-explorer`) | Browse and read files on the machine running Kiro Crew, in tabs, with git status and folder search. Read-only by design. |
| **AWS Control** (`aws-control`) | Your cloud accounts in plain language, plus an S3-backed cloud drive with time-boxed share links. |
| **Command Bar** (`command-bar`) | Replaces quick-search (`Cmd+K` / `Ctrl+K`) with a launcher: type a command, not a query. |

### Company while you work

| App | What it does |
|---|---|
| **Mochi** (`mochi`) | A desktop companion that lives on your screen, watches pages and feeds for you, and taps you on the shoulder only when something changed. |
| **Crew Companion** (`crew-companion`) | Paces your day: break nudges, plain-language reminders, a short breathing exercise, and session-finished alerts. |
| **Agent Worlds** (`agent-worlds`) | Turns your running agents into characters in an animated pixel-art scene, so a glance tells you how busy Kiro Crew is. |
| **Personal Shopper** (`personal-shopper`) | A personal advisor that researches real stores. It never buys anything — you do. |

Mochi and Crew Companion need the Kiro Crew **desktop app** for their own windows;
enabling them from a browser works, but the window does not appear. Channels and
Workflows are deliberately hidden from Discover and Library while disabled — reach
them through the features that use them, or enable them from the CLI.

## Where an app's pages live

A shipped app's page is at the route its manifest declares — Mochi's is `/mochi` —
and an app you installed yourself is hosted at `/apps/:name`. Either way the sidebar
entry is created for you; you do not need to know the route. Command Bar is the one
shipped app with no page of its own: it takes over quick-search rather than adding a
destination.

## Agents, skills and MCP tools an app brings

Enabling an app can add to your **agent picker** and your skill list, because an app
may ship its own. Registration happens on enable and is undone on disable.

- **Agents.** Meetings ships three (a note taker, a sketch artist, a task
  extractor), PPTX Maker four, Mochi two, Auto-Improvement four, Personal Shopper
  one. They are registered under the app's own namespace, so two apps can ship an
  agent with the same name without colliding.
- **Skills.** Code Review Sage, Design Critique, Design Tweak, Spec Builder,
  Personal Shopper, Auto-Improvement, Dev Fleet and Mochi each add one or more. They
  behave exactly like the skills in [skills.md](skills.md).
- **MCP tools.** Mochi and Auto-Improvement each run their own MCP server, and its
  tools are merged into the agents that are allowed them.
- **Scheduled jobs.** Ops Mission Control declares crons, which are registered on
  enable and removed on disable.

An app can also declare command-line tools it wants on your `PATH` — `gh` for Code
Review Sage, `pdflatex` or `tectonic` for Papyrus, `aws` and `gh` for Ops Mission
Control, `gh` / `glab` / `az` for Issue Radar. A missing one never blocks the
install: it is reported so you can see what is unmet, and only the part of the app
that needs it stops working.

## Permissions and trust

**Enabling an app runs its code with the same privileges as Kiro Crew itself.** App
Python is loaded into the gateway process; the manifest's `permissions` block gates
the SDK tool surface handed to the app, not its imports, filesystem, network or
subprocess use. There is no process-level sandbox around app code. So the honest rule
is the simple one: only enable apps you trust.

Kiro Crew makes the boundary explicit rather than pretending it is not there:

- **Shipped apps are trusted like core.** Anything loaded from outside the package is
  third-party.
- **Third-party execution is deny-by-default.** A third-party app is refused until
  you grant it trust. The refusal becomes a consent dialog naming what the app
  receives; confirming it grants trust to **that app only** and retries the action.
- **Turning trust off revokes it.** The app's tracked backend process is stopped —
  at the moment you change the setting, at the next gateway start, and by a periodic
  liveness check that re-reads the policy. It is never a label that changes nothing.
- **Every app load is audited** in the Security Event Log with its trust class.

The full boundary, including what revocation cannot reach, is in
[the app platform trust model](https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/app-platform-trust-model.md)
— a contributor document in the repository, and the source of truth for this section.

## Updates and migration

Discover flags an app whose catalog version is newer than yours. `/apps/-/updates`
collects every one of those into a worklist with **Update All**; Library shows a
one-line hint pointing there, and each tile keeps its own Update button.

`/apps/migrate/:name` handles a different case: an app that used to ship inside the
package and now lives outside it. The page tells you whether to install the external
version, or clean up what the old one left behind. Your app data is not touched
either way.

## From the command line

```bash
kirocrew app list                      # installed apps
kirocrew app info meetings             # one app's details
kirocrew app enable meetings
kirocrew app disable meetings
kirocrew app install /path/to/my-app   # install a local app directory
kirocrew app uninstall my-app          # keeps app data
kirocrew app uninstall my-app --purge-data   # deletes it permanently
```

Four more subcommands exist for people building apps rather than using them:
`import` converts a manifest-declared plugin package into an app directory, `init`
scaffolds a new app, `dev` toggles dev mode (no-store UI serving plus live reload on
file change), and `mcp` runs an app's MCP server on stdio — kiro-cli spawns that one,
you do not type it.

## Writing your own

The App Kit is the developer documentation: scaffolding, every `app.json` field, the
SDK surface, publishing, and migrating across a breaking platform version. It lives
in the repository, not in this package —
[docs/app-kit/](https://github.com/kirodotdev/KiroCrew/blob/main/docs/app-kit/README.md).

## See also

- [MCP Apps](mcp-apps.md) — a different feature with a similar name: rendering an
  MCP tool's interactive output inside a chat message.
- [Dashboard](dashboard.md) — the pages an app's own page sits beside.
- [Skills](skills.md) and [Agents](agents.md) — what an app's skills and agents join.
