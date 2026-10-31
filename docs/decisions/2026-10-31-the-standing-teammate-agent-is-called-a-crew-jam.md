# The standing teammate agent is called a Crew Jam, shown as a Jam, untranslated in every language

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-10-31
Supersedes: 2026-10-15-the-standing-teammate-agent-is-called-a-mate.md

## Decision

The standing teammate agent's full name is **Crew Jam**, and its short name is **Jam** (plural **Jams**). Everywhere a person reads the short name "Mate" today, it reads **Jam**, and "Mates" reads **Jams**: the Jams page, its roster, the create, edit and delete dialogs, the chat header, the settings rows and the user-facing docs. "Crew Jam" appears only where the full name "Crewmate" is used today; "Crew" is not put in front of every use. Neither name is translated in any catalog: zh-CN shows "Jam" / "Jams" in place of 队友, and ja, ko, de and every other catalog show the same English names. The reports folder follows the page name: `Jams/{name}/Reports` replaces `Crewmates/{name}/Reports`, and existing folders are moved once. The export file follows the full name, as `.crewmate` did: it is written as `.crewjam`, with format id `kirocrew.crewjam` and inner manifest `crewjam.json`; import still accepts a `.crewmate` file with format id `kirocrew.crewmate` and `crewmate.json`. Code identifiers, i18n keys, API paths, config keys, routes and persisted `crewmate_*` state keep their current spelling. The name of the default seeded Jam ("Mate") is not decided by this entry.

## Why

- The maintainer chose a new product name: "Crew mate有个新的名字 Crew Jams" (Crew mate has a new name, Crew Jams).
- "Jams" has no good Chinese translation: "Jams太难有中文名字了" (Jams is too hard to give a Chinese name). So one untranslated name is used in every language, instead of the per-language teammate words the 2026-10-15 entry kept.
- The maintainer then set the split: full name "Crew Jam" where "Crewmate" was used, short name "Jam" / "Jams" where "Mate" / "Mates" was used, so the short UI label stays one word as the 2026-10-15 entry wanted.
- The reports folder and the export file are read by a person, so they carry the new names too: the folder matches the page name, the file matches the full name. Import keeps reading the old form so files exported before the rename still open.
- As in the 2026-10-15 entry, renaming code identifiers and persisted state would touch hundreds of files for a label change, so they keep their spelling.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/26490 -- the RFC that plans the rename: scope, export format, reports-folder migration and wave order.
- The pull request that adds this entry, where the maintainer's override records the decision.
