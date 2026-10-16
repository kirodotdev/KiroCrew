# The standing teammate agent is called a Mate, not a Crewmate

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-15

## Decision

Everywhere a person reads it, the standing teammate agent is a **Mate** (plural **Mates**): the Mates page, its roster, the create, edit and delete dialogs, the chat header, the settings rows and the user-facing docs. Translations keep the teammate sense they already carry (队友, compañero, coéquipier, colega, напарник, compagno, साथी, সঙ্গী); a catalog that today uses a loanword for the English term (クルーメイト, 크루메이트, German Crewmate) keeps it. Code identifiers, i18n keys, API paths and data-file names keep the `crewmate` spelling.

## Why

- "Crewmate" is long, and its first half repeats the product name. "Mate" says the same thing in one syllable.
- The self-record MCP server was already named `kirocrew-mate`, so the surface and the plumbing disagreed.
- Renaming the identifiers would touch about 700 files and persisted `crewmate_*` state for a label change, so the word changes only where a person reads it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/20937 -- the issue recording the scope: copy and docs only, translations keep the crewmate/teammate feel.
- https://github.com/kirodotdev/KiroCrew/pull/20965 -- the pull request that adds this entry and applies the rename; the maintainer restates the decision in a comment there.
