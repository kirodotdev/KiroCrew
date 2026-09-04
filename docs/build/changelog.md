# Writing a CHANGELOG.md section

`CHANGELOG.md` is written **only when a version is bumped**, and everything already
in it is immutable. This doc is the spec for the section itself: when it is written,
what shape it takes, and what the format budget refuses. Cutting the release the
section belongs to is [release.md](release.md).

Two halves enforce the rules. The `changelog-is-written-at-version-bump-only` rule in
`AUTOSDE.yaml` applies the judgment a reviewer has to make (is this a commit dump?
should this PR be touching the file at all?), and
`scripts/check_changelog_history.py` enforces what needs no reading: every section the
base documents as shipped survives byte-identical, and the file contains **only**
shipped sections. The parser that renders it into the dashboard is
`src/kiro_crew/changelog.py`.

## When it is written

- **Your feature PR does not touch `CHANGELOG.md`.** The release PR writes the
  section covering everything that shipped. A per-PR changelog line is how the file
  grows into something nobody reads, and how it acquires an `## [Unreleased]` section
  that then has to be untangled at release time. The commit subject is the record
  until a bump names it.
- **There is no `## [Unreleased]` section, and the gate refuses one.** To see what is
  pending, read `git log --oneline <last-tag>..HEAD`. With shipped sections frozen,
  that leaves exactly one legal shape for a changelog diff — prepend one new section —
  because there is nowhere to append a per-PR line to.
- **Never delete or edit a shipped section.** A release PR prepends one section and
  leaves every earlier one byte-identical. This has already gone wrong once: a section
  was *replaced* rather than prepended and 322 lines of released history went with it,
  which no test caught and a user reported as an empty Releases page.

## The heading shape

**One section per release, newest first**, headed exactly `## [X.Y.Z] - YYYY-MM-DD`
with a plain hyphen. The parser also accepts the em and en dashes older sections
carry, but new sections do not use them.

Never a prerelease spelling: `0.3.0-insider.9` and `0.3.0-rc.2` are drafts of `0.3.0`,
are folded onto it by the parser, and must not get their own heading. The gate refuses
those too, so a release branch writes its section once under the release's final
heading rather than carrying a draft it renames later. A stable release must also
never ship a version number carrying a prerelease suffix at all; that constraint and
its one escape hatch are in [release.md](release.md).

## Format

The `[0.6.0]` section is the reference.

- A one-to-three sentence opening paragraph naming the release's theme. Not a count of
  commits.
- Then `###` subsections grouped by **what the reader gets**, ordered most interesting
  first. Never group by commit type: nobody opens a changelog looking for the
  refactors.
- **Three sentences per subsection, at most.** A subsection is up to three bullets of
  one sentence each, or one short paragraph. The budget is what forces the edit: the
  three things a reader of that area most needs to know survive, the rest is folded
  into one of them or dropped. Past three, split the theme or cut.
- Each bullet is `- **Short name**: what the user can now do`, one sentence, in plain
  language and the present tense.
- **No em dashes or en dashes anywhere in the section**, in headings, bullets or prose.
  Use a colon after the bolded name, a comma or a full stop elsewhere.
- Describe the capability, not the mechanism. No commit hashes, PR numbers, file paths,
  module names, or internal vocabulary.
- **Never generate the section from a commit dump.** A list of commit subjects, a
  `Bug Fixes (88 total)` header, or a trailing `and 65 more (see commit log)` is the
  failure mode this format exists to prevent. Fixes that are invisible to the reader
  are simply left out; fixes that are visible are described as an outcome and folded
  into the subsection they belong to.
- **The budget, not the range, decides the length.** A release covering twice the
  commits still gets three sentences per subsection; it earns more subsections only
  when it shipped more distinct things a reader gets. The showcase body carries new
  surfaces, new capabilities, and perceptible performance changes; everything
  fix-shaped goes to the grouped tail.

## Say where every feature lives, and how it is turned on

Name the page, panel and control, the composer button, the CLI command or the config
key in the same sentence ("under Developer → Agent Backend", "the composer's + menu
gains a Sketch row", "`kirocrew config defaults --adopt`"), and name the gate when
there is one (Developer Mode, Feature Previews, a default-off setting, a required
binary).

Verify each location in the code, not from the commit message: a panel that sits
behind Developer Mode, a component that is never mounted, or a label that differs from
the commit subject are all things a first-pass draft gets wrong. A reader who cannot
find the switch did not get the feature. This applies to default-on features too — a
shipped-by-default surface is just as undiscoverable when the section never names
where it is.

A subsection whose whole surface sits behind Developer Mode or Feature Previews
carries `(Preview)` at the end of its heading, so the reader knows before the first
bullet that it is opt-in.

## The two named sections

- **`### Before you upgrade` leads** when the release removes a capability, raises a
  floor (a minimum Node or Python version), changes a default, or alters behaviour a
  user has configured around. This is the part of a changelog with no substitute: a
  reader can discover a new feature later, but a withdrawn one costs them an outage.
- **A closing `### Notable fixes` section is allowed, and is not a commit dump.** It
  exists so a reader can check whether their particular annoyance is gone, written as
  what is now true ("Teams retries a rate-limited message instead of dropping it").
  Group the fixes by area into prose paragraphs under a bolded lead
  (`**Chat and sessions.** ...`), and hold each paragraph to the same three-sentence
  budget as a subsection. What makes it a dump instead is a total count, a bare commit
  subject, a scope prefix, or an "and N more" tail; it carries none of those, and a fix
  nobody would notice does not earn a line.
- **No `### Contributors` section.** The GitHub Release page renders its own
  contributor block from the tag range, natively, so a list in the changelog
  duplicates it. Older sections carry one; do not add one to a new section, and do not
  hand-write one into the release notes either.

## Verify coverage against the commit range

Partition `git log <last-tag>..HEAD` and account for every commit, because the
omissions are systematic rather than random: a change whose subject names one subsystem
while touching a shared surface is exactly what a keyword or path scan misses, and
nothing downstream ever reports it.

It is also systematically biased *against* the release's headline: the PR that lands a
large surface is the least likely to have spent effort on a changelog line, so an
accumulated file over-represents small fixes and omits the features people upgraded
for.

Accounting for a commit does not mean writing it up. The budget above decides what
survives, and the rest is a deliberate, recorded cut.
