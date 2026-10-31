---
title: Rename the Mate to the Jam (full name Crew Jam) — the word, the export file and the reports folder
status: accepted
author: iamwhatever, with kirocrew-worker
created: 2026-10-31
last-audited: 2026-10-31
audited-at: fea0920bf8
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Rename the Mate to the Jam (full name Crew Jam)

- Status: accepted on the repository owner's decision of 2026-10-30. Nothing is built on main yet. This document lands first so every later wave can be checked against one list of what changes and what stays.
- Claims about today's code were checked at `fea0920bf8` (main).
- Replaces, once its own entry lands (§8, W0b): [docs/decisions/2026-10-15-the-standing-teammate-agent-is-called-a-mate.md](../decisions/2026-10-15-the-standing-teammate-agent-is-called-a-mate.md). That entry is immutable and is not edited here.
- Related: [#20965](https://github.com/kirodotdev/KiroCrew/pull/20965), the earlier rename from Crewmate to Mate, whose layer split this document reuses. [rfc-crewmate-share.md](rfc-crewmate-share.md) §5.2 to §5.4 (the `.crewmate` file) and [rfc-crewmate-goals.md](rfc-crewmate-goals.md) (reports) are amended here, in this document, so those accepted documents are not edited.

## 1. Summary

The standing teammate agent is renamed everywhere a person reads it. Its full name **Crewmate** becomes **Crew Jam**. Its short UI name **Mate** (plural **Mates**) becomes **Jam** (plural **Jams**), so the Mates page becomes the Jams page. Both are brand words: they are not translated in any language. Two names a person handles also change: the export file becomes `.crewjam` (it follows the full name, as `.crewmate` did), and reports are filed under `Jams/<name>/Reports` (it follows the page name). Old files still import, and old report folders move once. Code names, API fields, config keys, routes and card kinds keep their spelling.

## 2. Today

Verified at `fea0920bf8`:

1. The English word is **Mate / Mates** (decision of 2026-10-15). Examples: `pages.membersPage.title` = "Mates", `pages.membersPage.add_member` = "New mate" in `website/src/i18n/locales/en.json`. The nav label's machine value is `surfaceMachineValue('Mates')` on the `members` entry of `website/src/surfaces/surfaceData.ts`.
2. Translations keep a teammate word: zh-CN says 队友 (229 values), ja クルーメイト, ko 크루메이트, de Crewmate.
3. Export writes a zip named `<name>.crewmate` (`bundle_filename` and `content_disposition` in `src/kiro_crew/crewmate_bundle_export.py`). Its manifest entry is `crewmate.json` (`_fixed_entries`, same file) and carries `"format": CREWMATE_FORMAT`, where `CREWMATE_FORMAT = "kirocrew.crewmate"` (`src/kiro_crew/crewmate_bundle.py`).
4. Import reads only that form. In `crewmate_bundle.py`, `_JSON_ENTRIES` maps the entry `crewmate.json` to the part `crewmate`; `read_crewmate_file` requires `crewmate.json` and `agent.json` and refuses a bad zip with "this is not a .crewmate file" (three places); `crewmate_import_params` refuses any other `format` with the same text. `formatVersion` must be in `CREWMATE_FORMAT_VERSIONS = (1,)` (`src/kiro_crew/change_card_catalog_kinds.py`). The file picker filters on `CREWMATE_FILE_ACCEPT = '.crewmate'` (`website/src/pages/members/CrewmateImportDialog.tsx`).
5. Reports are filed under `REPORTS_FOLDER = "Crewmates/{name}/Reports"` (`src/kiro_crew/dashboard/goal_reminder.py`), which `save_report` resolves with `resolve_path(folder_path, create_missing=True)`. The folder is a row in `ArtifactFolderStore` (`src/kiro_crew/artifact_store/folders.py`); an artifact points at it by `folder_id`. The agent is told the path in the `save_goal_report` description (`_tool_definitions` in `src/kiro_crew/mcp_mate.py`). Docstrings repeat it in four places: the `goal_reminder.py` module docstring and `save_report`, `api_member_self_goal_report` in `src/kiro_crew/dashboard/handlers/member_goals.py`, and the `src/kiro_crew/member_goals.py` module docstring.
6. The seeded first crewmate is named "Mate" (`ASSISTANT_DEFAULT_DISPLAY_NAME` in `src/kiro_crew/agent_files.py`). That is one crewmate's name, not the category word.

## 3. Decision

### 3.1 The word

| Where | Today | After |
|---|---|---|
| Short name, one | Mate / mate | Jam |
| Short name, many | Mates / mates | Jams |
| Full name, one | Crewmate / crewmate | Crew Jam |
| Full name, many | Crewmates / crewmates | Crew Jams |
| Page title, nav label | Mates | Jams |
| Reports folder | Crewmates | Jams |
| Export file | `.crewmate` | `.crewjam` |

1. Every "Mate" becomes "Jam" and every "Mates" becomes "Jams" ("New mate" → "New Jam", "No mates yet" → "No Jams yet", the Mates page → the Jams page). "Crew" is not put in front of these.
2. "Crew Jam" appears only where today's text says the full name "crewmate". In the English catalogs on main that is 2 values, both in `en.json`: "This crewmate runs on {{harness}}…" and "Private to this crewmate". Two more values only name the file: "Share this mate as a .crewmate file" (`en.json`) becomes "Share this Jam as a .crewjam file", and the import hint in `en.manual.json` that says ".crewmate file" and "Import mate" becomes ".crewjam file" and "Import Jam".
3. A brand keeps one spelling, so "Jam", "Jams" and "Crew Jam" are capitalised mid-sentence too.
4. Every language uses "Jam" / "Jams" / "Crew Jam" verbatim, zh-CN, ja, ko and de included. 队友, クルーメイト, 크루메이트 and the German Crewmate are replaced wherever they stand for the category. The rest of each sentence stays translated.
5. "Jam", "Jams" and "Crew Jam" join the do-not-translate list in `website/src/i18n/glossary.json`, so `glossary.test.ts` and `scripts/check-dnt-catalogs.mjs` hold every catalog to them. No catalog value on main contains the word "jam", so the list adds no false match.
6. The seeded crewmate's own name "Mate" stays. It is a name, like "Ada". A string that names that one crewmate stays: `pages.membersPage.built_from_mate_label` = "Mate" (read in `website/src/components/crew/builtFromName.ts`). A string about the category changes, including `pages.membersPage.entrance_first_mate` = "Hi! I'm your first Mate." → "Hi! I'm your first Jam."

### 3.2 The export file

1. Export writes only the new form: file `<name>.crewjam`, manifest entry `crewjam.json`, `"format": "kirocrew.crewjam"`.
2. Import accepts both forms. A file is valid in exactly one of these shapes:

| Form | Manifest entry | `format` |
|---|---|---|
| new | `crewjam.json` | `kirocrew.crewjam` |
| old | `crewmate.json` | `kirocrew.crewmate` |

3. A zip that holds both entries, or whose `format` does not match its entry's form, is refused `invalid_bundle`. Every other check runs the same on both forms: the size cap, entry list, `agent.json` required, `formatVersion` in `CREWMATE_FORMAT_VERSIONS`, key allow-lists, redaction, carried-file checks. Tests cover each check on both forms.
4. Both forms parse into the same part `crewmate` (the key in `_BUNDLE_KEYS`, `crewmate_bundle.py`). That key is what an app hands `ctx.crewmate`, so it is an API field and stays.
5. The file picker accepts `.crewjam,.crewmate`. The refusal reads "this is not a .crewjam file".
6. `formatVersion` stays `1` for both forms. Only the names change; the content does not.

### 3.3 The reports folder

1. `REPORTS_FOLDER` becomes `"Jams/{name}/Reports"`, the page's name. The `save_goal_report` text in `mcp_mate.py` and the four docstrings in §2 item 5 say the same.
2. One-time migration at gateway start, guarded by a marker so it runs once:
   - It acts only on a top-level folder named exactly `Crewmates`.
   - No top-level `Jams`: rename `Crewmates` to `Jams`.
   - `Jams` exists: move each child of `Crewmates` into it. Two children with the same name merge the same way, one level down. The emptied `Crewmates` folder is then deleted.
   - It changes folder rows and artifact `folder_id` only. No report body is read or written.
   - It does not start folder icon generation (the epoch that `ArtifactFolderStore.rename` bumps must not arm one).
   - It is safe to run again: a second run finds no `Crewmates` folder and does nothing.

### 3.4 What does not change

These keep their spelling, so later waves can be checked with a grep:

- Code symbols, module and file names: `crewmate_*`, `CREWMATE_*`, `Crewmate*` components, `mate_*`, `members/`.
- i18n keys (`pages.membersPage.*`, `*_crewmate*`). Only values change.
- API paths and fields: `/api/members/...`, `/api/agents`, `ctx.crewmate`, the bundle part key `crewmate`.
- Config keys and stored data: `member_id`, `crewmate_*` fields, `~/.kiro/mates/`, `config-baseline.json` keys.
- URL routes: `/members`.
- Change-card kinds: `crewmate.create`, `crewmate.update`, `crewmate.import` and the rest.
- MCP server and tool names: `kirocrew-mate`, `save_goal_report` and the rest.
- The staged import copy's suffix `SUFFIX = ".crewmate"` (`src/kiro_crew/crewmate_stage.py`). It lives under `run/` and nobody sees it.
- Historical RFCs and every `docs/decisions/` entry.
- The seeded crewmate's name "Mate" (§3.1 item 6).

## 4. Scope inventory

Measured at `fea0920bf8` with the commands in §4.1. The word is `\b(crew ?mates?|mates?)\b`, case-insensitive.

| Area | Count | Wave |
|---|---|---|
| `en.json` values | 107 | W1 |
| `en.manual.json` values | 135 | W1 |
| English values that use the full name "crewmate" (→ Crew Jam) | 2 | W1 |
| `en.context.json` lines (translator notes) | 62 | W1 |
| Translated catalogs (bn, de, es, fr, hi, it, ja, ko, pt, ru, zh-CN) | 11 files, 236 to 242 of those keys each | W2 |
| `en-XA.json` pseudolocale | regenerated | W1 |
| `glossary.json` do-not-translate list | 3 terms added (Jam, Jams, Crew Jam) | W2 |
| Hardcoded visible frontend string | 1: the `members` label in `surfaceData.ts` | W1 |
| Search aliases (`website/src/uiLocations/areas/*.ts`) | 66 lines | W1 |
| Command bar keywords (`command:search-mates` in `CommandBarOverlayRootRows.tsx`) | 1 line | W1 |
| Frontend tests asserting copy (`*.test.tsx`, `*.test.ts`) | 46 files, 126 lines | W1 |
| Capture scripts asserting copy (`website/scripts/capture-*`) | 16 files, 47 lines | W1 |
| Backend string constants with the word, not docstrings | 393 | W1 classifies |
| Built-in dashboard templates (`template.html`, `manifest.json`) | 19 files | W1 |
| Settings registry ids derived from a changed label | 2 | W1 |
| Export format names | §3.2 | W3b |
| Reports folder and its texts | §3.3 | W3a |
| User docs `src/kiro_crew/docs/*.md` | 11 of 57 files, 171 lines | W4 |
| `website/docs` | 3 files | W4 |
| `src/kiro_crew/docs/ui-index.generated.json` | 88 lines, regenerated | W4 |

Notes:

1. English lives in two files. `en.json` is regenerated by `scripts/i18n-codemod.mjs`; `en.manual.json` is hand-written (`website/src/i18n/enCatalog.ts`). Both count. The two files share no key.
2. There are 14 files under `website/src/i18n/locales/`: 12 languages (`en` plus 11 translated, `SUPPORTED_LANGUAGES` in `website/src/i18n/languages.ts`), the dev-only `en-XA` pseudolocale, and `en.manual.json`.
3. Only the `members` label in `surfaceData.ts` draws visible text outside the catalogs. Search aliases and command bar keywords are search terms. They gain "jam" and "crew jam" terms and keep the old words, because people still type them.
4. Of the 393 backend constants, a string changes only if a person reads it: change-card titles and refusals, error text shown in the dashboard, notifications, settings labels and help, guide titles, goal-reminder skip reasons, agent-facing text that names a folder or file the person sees. Log lines do not change. The biggest user-facing files: `change_card_catalog_plans.py` (18), `config/sections_session.py` and `config/sections_dashboard.py` (13 together), `dashboard/goal_reminder.py` (11), `guide_catalog.py` (9), `member_export.py`, `crewmate_import_files.py`, `member_self.py`, `member_goals.py`, `crew_teams.py` (6 each), `crewmate_bundle.py` (4).
5. Settings registry ids come from labels. Two move: `developer.agent-harness-per-mate` → `developer.agent-harness-per-jam` and `developer.reply-threads-on-mate-chat-messages` → `developer.reply-threads-on-jam-chat-messages` (`src/kiro_crew/docs/settings-registry.generated.json`). W1 adds each old id to `LEGACY_ID_EXACT` (`website/src/hooks/useSettingHighlight.ts`), as #20965 did.
6. `CHANGELOG.md` has no unreleased section; its top is `[0.11.0]`. W4 hands the line to whoever opens the `0.12.0` section, or adds it there if that section exists.

### 4.1 Commands

```bash
W='\b(crew ?mates?|mates?)\b'
# catalog leaf values matching the word, per file
for f in en en.manual; do python3 -c '
import json,re,sys
def leaves(d):
    if isinstance(d,dict):
        for v in d.values(): yield from leaves(v)
    elif isinstance(d,str): yield d
rx=re.compile(r"\b(crew ?mates?|mates?)\b",re.I)
print(sys.argv[1],sum(1 for v in leaves(json.load(open(sys.argv[1]))) if rx.search(v)))
' "website/src/i18n/locales/$f.json"; done
git grep -cEi "$W" -- website/src/i18n/en.context.json
git grep -nEi "$W" -- website/src/uiLocations | wc -l
P="(Text|name:|has-text\(|text=)\s*\(?\s*['\"/\`][^'\"\`]*\b([Cc]rew ?)?[Mm]ates?\b"
git grep -lE "$P" -- 'website/scripts/capture-*' | wc -l
git grep -lE "$P" -- 'website/src/**/*.test.tsx' 'website/src/**/*.test.ts' | wc -l
git grep -lEi "$W" -- 'src/kiro_crew/**/template.html' 'src/kiro_crew/**/manifest.json' | wc -l
git grep -lEi "$W" -- 'src/kiro_crew/docs/*.md' | wc -l
git grep -cEi "$W" -- src/kiro_crew/docs/ui-index.generated.json
```

The backend count walks every `src/kiro_crew/**/*.py` with `ast`, skips docstrings, and keeps string constants that contain a space and match the word.

## 5. Correctness rule for later waves

A wave is correct when:

1. Every string it changes is in §4, and every identifier in §3.4 is byte-identical at its head.
2. After W1 and W2, no catalog value matches `\bmates?\b` or `\bcrew ?mates?\b`, except the seeded name in §3.1 item 6. "Crew Jam" appears only in the values §3.1 item 2 names; every other changed value says "Jam" or "Jams".
3. After W2, every catalog writes "Jam", "Jams" and "Crew Jam" in Latin letters, and no value contains 队友, クルーメイト, 크루메이트 or the German "Crewmate" where it means the category.
4. After W3b, export writes only `.crewjam`, and the import tests in §3.2 item 3 pass on both forms.
5. After W3a, reports go under `Jams/`, no code writes under `Crewmates/`, and the migration tests cover rename, merge and second run.

## 6. Alternatives considered

- **Translate the word.** Rejected by the owner: the name is a brand.
- **Keep `.crewmate` and the `Crewmates` folder.** Rejected by the owner: a person sees both names, so they would contradict the page.
- **Rename code, keys and routes too.** Rejected: about 12,000 references and stored state for a label change. #20965 drew the same line.
- **Rename the seeded "Mate".** Not taken here: it is one crewmate's name, and #20965 also left it.

## 7. Risks

- **Old files.** People hold `.crewmate` files. Import keeps reading them, under the same checks (§3.2).
- **A second form is a second path into the reader.** Mitigation: one code path picks the form, then the same checks run; tests prove each check on both forms.
- **Do-not-translate near-miss.** `scripts/check-dnt-catalogs.mjs` flags respellings. A catalog value that says `.crewjam` may read as one. W2 checks this and, if needed, writes the extension in a way the gate accepts.
- **Folder merge.** A user may have made their own `Crewmates` folder. The migration moves its contents too. Nothing is deleted except the emptied folder row.
- **Older installs cannot read new files.** An install from before W3b checks for `crewmate.json` and `kirocrew.crewmate`, so it refuses a `.crewjam` file. This is accepted: the owner chose to write only the new form. The person who receives the file updates first.
- **Downgrade.** The migration does not run backwards. After a downgrade, the old `save_report` recreates `Crewmates/<name>/Reports` beside the moved folder and files new reports there. Because the migration runs once per install, a later upgrade leaves those reports where they are; the person can move them by hand. This is accepted: no report is lost, only filed in two places.
- **Bookmarks to settings.** Two settings ids move; `LEGACY_ID_EXACT` keeps old links working.

## 8. Rollout

| Wave | What | After | Exit |
|---|---|---|---|
| W0 | This document | — | On main, `status: accepted` |
| W0b | Decision entry that supersedes the 2026-10-15 entry | W0 | Merged; a maintainer restates the decision on that PR and overrides the decisions rule, as `docs/decisions/README.md` rule 3 requires |
| W1 | English copy (`en.json`, `en.manual.json`, `en.context.json`, `en-XA`), the `surfaceData.ts` label, search aliases, backend strings, templates, settings id aliases, tests and captures | W0b | No English value matches the old word (§5) |
| W3a | Reports folder, migration, `save_goal_report` text, docstrings | W0b | §5 item 5 |
| W3b | `.crewjam` export, two-form import, picker filter | W0b | §5 item 4 |
| W2 | 11 translated catalogs, `glossary.json` | W1 | §5 item 3; i18n gates green |
| W4 | `src/kiro_crew/docs`, `website/docs`, `ui-index.generated.json` regenerated (`npm run gen:ui`), CHANGELOG line | W1, W3a, W3b | Docs say Jam / Jams, and Crew Jam only for the full name; generated index fresh |

W1, W3a and W3b run in parallel. W0b is listed because a reversed decision entry blocks any PR that changes the label, until its superseding entry lands.
