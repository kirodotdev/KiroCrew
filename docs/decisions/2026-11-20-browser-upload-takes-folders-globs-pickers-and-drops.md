# Browser upload takes folders, globs, file pickers and drops, and a site's act grant covers it

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-20

## Decision

The `browser` tool's `upload` op does all five of these:

1. A person's "act" site grant on a host (reading, clicking and typing, for this session) also covers `upload` on that host, so a later upload there shows no card. A "read" grant never covers it, and `js`, `cdp` and `http_get` still ask on every call. The act grant's label and the upload card say so.
2. `via: chooser` clicks a button and fills the file picker the click opens.
3. `via: drop` drops the files on a drop zone.
4. A folder path uploads its files to a folder field (`webkitdirectory`) or a drop zone, capped in file count and total size, with links skipped and protected files refused.
5. A glob such as `~/Pictures/*.png` is expanded, and each match is checked like a plain path, with the same caps.

Every earlier guard stays: the sensitive-path refusals, no links, the staged copy, the size caps and the SEL row per file. An app's `browserSites` grant still never covers `upload`.

## Why

- The owner's words, on 2026-11-20 at 16:50 PST, when the conductor asked which of the five upload gaps to build: "都做" (do all five). This overrides, for `upload` only, his earlier ruling that the browser work builds only what Browser Harness's own helpers do.
- The owner accepted the risk that comes with item 1: after an act grant on a site, a page there that injects an instruction can make the agent upload a non-sensitive local file with no card. The grant lasts this session only and covers one host, the op route re-reads the tab's host before the files go, and the caps and the SEL rows stay.

## Evidence

- The ruling was given by Bolin Chen in the conductor chat that dispatched this work, on 2026-11-20 at 16:50 PST, quoted above verbatim and restated on the pull request: https://github.com/kirodotdev/KiroCrew/pull/40375#issuecomment-7060021860
