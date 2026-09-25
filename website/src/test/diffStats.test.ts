import { describe, it, expect } from 'vitest'
// Both from `utils/diffLineCounts`, not from the component/page that re-exports
// them: importing those pulled the Pierre diff runtime, framer-motion,
// react-markdown, katex and highlight.js into this fork — measured 144.65s, of
// which 51ms was the tests.
import { createTwoFilesPatch } from 'diff'
import { countLines, countDiffStats, changedLineSpan, splitPatchSections, plainPatchHunks } from '../utils/diffLineCounts'

describe('countLines (diff stats)', () => {
  it('returns zeros for identical content', () => {
    expect(countLines('hello', 'hello')).toEqual({ added: 0, removed: 0 })
  })

  it('counts added lines for new file', () => {
    const { added, removed } = countLines('', 'line1\nline2\nline3')
    expect(added).toBe(3)
    expect(removed).toBe(0)
  })

  it('counts removed lines for deleted content', () => {
    const { added, removed } = countLines('line1\nline2\nline3', '')
    expect(added).toBe(0)
    expect(removed).toBe(3)
  })

  it('counts both added and removed for modifications', () => {
    const { added, removed } = countLines('old1\nold2\nkeep', 'keep\nnew1\nnew2\nnew3')
    expect(added).toBeGreaterThan(0)
    expect(removed).toBeGreaterThan(0)
  })

  it('handles single line change', () => {
    const { added, removed } = countLines('before', 'after')
    expect(added).toBe(1)
    expect(removed).toBe(1)
  })
})

// Test the countDiffStats from ActivityViewer (parsing unified diff output)
describe('countDiffStats (unified diff parsing)', () => {

  it('returns zeros for empty diff', () => {
    expect(countDiffStats('')).toEqual({ added: 0, removed: 0 })
  })

  it('counts added lines from unified diff', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 keep
+new line 1
+new line 2
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 0 })
  })

  it('counts removed lines from unified diff', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,4 +1,2 @@
 keep
-removed 1
-removed 2
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 0, removed: 2 })
  })

  it('counts both added and removed', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,3 @@
 keep
-old line
+new line
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 1, removed: 1 })
  })

  it('ignores --- and +++ header lines', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1 +1 @@
-old
+new`
    expect(countDiffStats(diff)).toEqual({ added: 1, removed: 1 })
  })

  // Headers are found by POSITION, never by prefix: content can start with the
  // header prefixes too, and the diff worker's own producer marks it that way.
  it('counts a removed `-- comment` line the way createTwoFilesPatch emits it (`--- comment`)', () => {
    const before = 'SELECT 1;\n-- retired note\nSELECT 2;\n'
    const after = 'SELECT 1;\nSELECT 2;\n'
    const patch = createTwoFilesPatch('q.sql', 'q.sql', before, after, undefined, undefined, { context: 3 })
    expect(patch).toContain('\n--- retired note\n') // the producer's shape this guards
    expect(countDiffStats(patch)).toEqual({ added: 0, removed: 1 })
  })

  it('counts content lines that begin with the header prefixes inside a hunk', () => {
    const diff = `--- a/doc.md
+++ b/doc.md
@@ -1,4 +1,4 @@
 title: x
----
+--- rule
-i++
++++i
 end`
    // `----` is a removed markdown rule, `+++i` an added C-style increment.
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 2 })
  })

  it('does not count the second file header pair of a multi-file diff', () => {
    const diff = `diff --git a/one.ts b/one.ts
--- a/one.ts
+++ b/one.ts
@@ -1,2 +1,2 @@
 keep
-old one
+new one
diff --git a/two.ts b/two.ts
--- a/two.ts
+++ b/two.ts
@@ -1 +1 @@
-old two
+new two`
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 2 })
  })

  it('keeps the prefix rule for a fence with no hunk header', () => {
    expect(countDiffStats('-old\n+new\n+newer')).toEqual({ added: 2, removed: 1 })
    expect(countDiffStats('--- a\n+++ b\n-old\n+new')).toEqual({ added: 1, removed: 1 })
  })
})

describe('splitPatchSections (one section per file)', () => {
  const summary = (diff: string) =>
    splitPatchSections(diff).map(({ name, prevName, added, removed }) => ({ name, prevName, added, removed }))

  it('cuts a git multi-file diff at each `diff --git` preamble, keeping every line', () => {
    const one = `diff --git a/one.ts b/one.ts
index 1111111..2222222 100644
--- a/one.ts
+++ b/one.ts
@@ -1,2 +1,2 @@
 keep
-old one
+new one`
    const two = `diff --git a/two.ts b/two.ts
--- a/two.ts
+++ b/two.ts
@@ -1 +1 @@
-old two
+new two
`
    const sections = splitPatchSections(one + '\n' + two)
    expect(sections.map(s => s.text)).toEqual([one, two])
    expect(summary(one + '\n' + two)).toEqual([
      { name: 'one.ts', prevName: null, added: 1, removed: 1 },
      { name: 'two.ts', prevName: null, added: 1, removed: 1 },
    ])
  })

  it('cuts a preamble-less diff at the next header pair a `@@` announces', () => {
    const diff = `--- a.py
+++ a.py
@@ -1 +1 @@
-x = 1
+x = 2
--- b.py
+++ b.py
@@ -1,2 +1 @@
 keep
-gone`
    expect(splitPatchSections(diff).map(s => s.text.split('\n')[0])).toEqual(['--- a.py', '--- b.py'])
    expect(summary(diff)).toEqual([
      { name: 'a.py', prevName: null, added: 1, removed: 1 },
      { name: 'b.py', prevName: null, added: 0, removed: 1 },
    ])
  })

  it('names an addition, a deletion and a rename the way their headers do', () => {
    const diff = `--- /dev/null
+++ b/added.ts
@@ -0,0 +1 @@
+new
--- a/gone.ts
+++ /dev/null
@@ -1 +0,0 @@
-old
--- a/before.ts\t2026-01-01 00:00:00
+++ b/after.ts\t2026-01-02 00:00:00
@@ -1 +1 @@
-x
+y`
    expect(summary(diff)).toEqual([
      { name: 'added.ts', prevName: null, added: 1, removed: 0 },
      { name: 'gone.ts', prevName: null, added: 0, removed: 1 },
      { name: 'after.ts', prevName: 'before.ts', added: 1, removed: 1 },
    ])
  })

  it('does not cut at header-shaped content inside a hunk body', () => {
    const diff = `--- a/doc.md
+++ b/doc.md
@@ -1,4 +1,4 @@
 title: x
----
+--- rule
-i++
++++i
 end`
    expect(summary(diff)).toEqual([{ name: 'doc.md', prevName: null, added: 2, removed: 2 }])
  })

  it('does not cut at a lone `--- ` line past a miscounted hunk, only at a header pair', () => {
    // The hunk declares one old line but removes two; the second, `-- note`,
    // is content that a prefix rule would read as the next file's header —
    // and the hunk stays open past it, so the `+` line after it still counts.
    const diff = `--- a/q.sql
+++ b/q.sql
@@ -1,1 +1,1 @@
-SELECT 1;
--- note
+SELECT 2;`
    expect(summary(diff)).toEqual([{ name: 'q.sql', prevName: null, added: 1, removed: 2 }])
  })

  it('treats a fence with no hunk header as one section, named by its headers if any', () => {
    expect(summary('-old\n+new\n+newer')).toEqual([{ name: null, prevName: null, added: 2, removed: 1 }])
    expect(summary('--- a/x.ts\n+++ b/x.ts\n-old\n+new')).toEqual([{ name: 'x.ts', prevName: null, added: 1, removed: 1 }])
    expect(splitPatchSections('')).toEqual([{ text: '', name: null, prevName: null, kind: 'modified', binary: false, modeChange: null, body: [], added: 0, removed: 0 }])
  })

  /** A 100%-similarity rename is the one entry git writes with no hunk and no
   *  `---`/`+++` pair: its names live only on the `rename from` / `rename to`
   *  lines, so a section that read the header pair alone would title the row
   *  `new.ts` where Pierre's header says `old.ts → new.ts`. */
  it('names a 100%-similarity rename by its `rename from` / `rename to` lines', () => {
    const rename = `diff --git a/src/old.ts b/src/new.ts
similarity index 100%
rename from src/old.ts
rename to src/new.ts`
    expect(summary(rename)).toEqual([{ name: 'src/new.ts', prevName: 'src/old.ts', added: 0, removed: 0 }])
  })

  /** Entries with no hunk — a 100% rename, a binary change, a mode change — are
   *  files too. A cut that waited for a hunk or a `+++` header swallowed each
   *  into the NEXT file's section, so N files drew N−1 rows. */
  it('cuts at every `diff --git` line, giving hunk-less entries their own section', () => {
    const modified = `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-const a = 1
+const a = 2`
    const renamed = `diff --git a/src/old.ts b/src/new.ts
similarity index 100%
rename from src/old.ts
rename to src/new.ts`
    const binary = `diff --git a/assets/logo.png b/assets/logo.png
index 3333333..4444444 100644
Binary files a/assets/logo.png and b/assets/logo.png differ`
    const modeOnly = `diff --git a/bin/run.sh b/bin/run.sh
old mode 100644
new mode 100755`
    const diff = [modified, renamed, binary, modeOnly].join('\n')
    const sections = splitPatchSections(diff)
    expect(sections.map(s => s.text)).toEqual([modified, renamed, binary, modeOnly])
    expect(summary(diff)).toEqual([
      { name: 'src/a.ts', prevName: null, added: 1, removed: 1 },
      { name: 'src/new.ts', prevName: 'src/old.ts', added: 0, removed: 0 },
      { name: 'assets/logo.png', prevName: null, added: 0, removed: 0 },
      { name: 'bin/run.sh', prevName: null, added: 0, removed: 0 },
    ])
    // The same entries with the hunk-bearing file LAST: the cut cannot depend
    // on a hunk having opened the section before it.
    expect(summary([renamed, binary, modeOnly, modified].join('\n')).map(s => s.name))
      .toEqual(['src/new.ts', 'assets/logo.png', 'bin/run.sh', 'src/a.ts'])
  })

  /** Git writes a chmod as `old mode` / `new mode` lines in the entry's
   *  metadata, with or without a hunk beside them. The section carries the
   *  change as data so the row can state it; the plain body never has to. */
  it('reports a mode change from the metadata block, beside content or alone', () => {
    const chmodAndEdit = `diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
--- a/run.sh
+++ b/run.sh
@@ -1 +1 @@
-echo one
+echo two`
    const [section] = splitPatchSections(chmodAndEdit)
    expect(section.modeChange).toEqual({ from: '100644', to: '100755' })
    expect(section.added).toBe(1)
    expect(splitPatchSections('diff --git a/bin/run.sh b/bin/run.sh\nold mode 100644\nnew mode 100755')[0].modeChange)
      .toEqual({ from: '100644', to: '100755' })
    // A `new file mode` is not a change of mode.
    expect(splitPatchSections('diff --git a/x b/x\nnew file mode 100644\n--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+x')[0].modeChange).toBeNull()
  })

  /** A hand-written `@@ section @@` header carries no counts, but it is still
   *  where a hunk begins: everything after it is content by POSITION, so a
   *  deleted `-- foo` (`--- foo`) and an added `++ bar` (`+++ bar`) inside it
   *  are lines of this file, not the next file's header pair. The next file
   *  begins only where a header pair announces itself — by a `@@` right below
   *  it, or a `diff ` line above. */
  it('reads a non-numeric `@@` header as a hunk and keeps header-shaped content inside it', () => {
    const diff = `--- a/notes.sql
+++ b/notes.sql
@@ selection @@
 SELECT 1;
--- foo
+++ bar
 SELECT 2;
--- a/other.sql
+++ b/other.sql
@@ -1 +1 @@
-x
+y`
    expect(summary(diff)).toEqual([
      { name: 'notes.sql', prevName: null, added: 1, removed: 1 },
      { name: 'other.sql', prevName: null, added: 1, removed: 1 },
    ])
  })

  /** A hunk whose declared counts overshoot its body — a hand-edited or
   *  truncated patch — must not swallow the next file: a `---`/`+++` pair a
   *  `@@` announces begins a file wherever it stands, counts owed or not. That
   *  is the cut `normalizePatchHunks` makes for Pierre, so the rows and the
   *  bodies under them agree on where each file begins. */
  it('cuts at an announced header pair while a miscounted hunk still owes lines', () => {
    const diff = `--- a/x.ts
+++ b/x.ts
@@ -1,5 +1,5 @@
-a
+b
--- a/y.ts
+++ b/y.ts
@@ -1,5 +1,5 @@
-c
+d`
    expect(summary(diff)).toEqual([
      { name: 'x.ts', prevName: null, added: 1, removed: 1 },
      { name: 'y.ts', prevName: null, added: 1, removed: 1 },
    ])
  })

  /** The other side of the same coin: a hunk whose declared count UNDERSHOOTS
   *  its body (a model-written patch) followed by a deleted `-- foo` and an
   *  added `++ bar` — `--- foo` / `+++ bar` — must not read those two lines as
   *  the next file's header because the counts say the hunk is over: nothing
   *  announces a file there, so they are content, one section, every line
   *  counted and kept — the reading `normalizePatchHunks` gives Pierre, which
   *  repairs the stale counts from the body. */
  it('keeps header-shaped content under a hunk whose stale counts say it is over', () => {
    const diff = `--- a/q.sql
+++ b/q.sql
@@ -1,1 +1,1 @@
-SELECT 1;
+SELECT 2;
--- foo
+++ bar
 SELECT 3;`
    expect(summary(diff)).toEqual([{ name: 'q.sql', prevName: null, added: 2, removed: 2 }])
    expect(plainPatchHunks(diff)).toEqual(['-SELECT 1;\n+SELECT 2;\n--- foo\n+++ bar\n SELECT 3;'])
  })

  /** Git's other hunk-less entries: a copy (`copy from` / `copy to`, `git diff
   *  -C`) is named like a rename, and a `diff --git` line whose paths git had
   *  to quote (a space, a non-ASCII byte) names the file with the quotes and
   *  escapes undone. Neither has a `---`/`+++` pair to fall back on, so a row
   *  that could not name them would be an unnamed row over an empty body. */
  it('names a 100% copy and a quoted-path entry from their preamble', () => {
    const copy = `diff --git a/src/a.ts b/src/b.ts
similarity index 100%
copy from src/a.ts
copy to src/b.ts`
    expect(summary(copy)).toEqual([{ name: 'src/b.ts', prevName: 'src/a.ts', added: 0, removed: 0 }])
    expect(plainPatchHunks(copy)).toEqual([])
    const quoted = `diff --git "a/docs/my notes.md" "b/docs/my notes.md"
old mode 100644
new mode 100755`
    expect(summary(quoted)).toEqual([{ name: 'docs/my notes.md', prevName: null, added: 0, removed: 0 }])
    const escaped = `diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"
old mode 100644
new mode 100755`
    expect(summary(escaped)).toEqual([{ name: 'café.txt', prevName: null, added: 0, removed: 0 }])
    const quotedRename = `diff --git "a/old name.ts" "b/new name.ts"
similarity index 100%
rename from "old name.ts"
rename to "new name.ts"`
    expect(summary(quotedRename)).toEqual([{ name: 'new name.ts', prevName: 'old name.ts', added: 0, removed: 0 }])
  })

  /** What kind of change an entry is — added, deleted, modified — and whether
   *  it is binary, read off git's extended header lines and the header pair's
   *  `/dev/null` side. Data for the row, which is the only place left to state
   *  it once Pierre's own header (its change icon) is off. */
  it('reports the change kind and a binary entry from the metadata block', () => {
    const kinds = (diff: string) => splitPatchSections(diff).map(({ kind, binary }) => ({ kind, binary }))
    expect(kinds('diff --git a/n.ts b/n.ts\nnew file mode 100644\n--- /dev/null\n+++ b/n.ts\n@@ -0,0 +1 @@\n+x')).toEqual([{ kind: 'added', binary: false }])
    // difflib's shape has no preamble: the `/dev/null` side alone says it.
    expect(kinds('--- /dev/null\n+++ b/n.ts\n@@ -0,0 +1 @@\n+x')).toEqual([{ kind: 'added', binary: false }])
    expect(kinds('diff --git a/g.ts b/g.ts\ndeleted file mode 100644\n--- a/g.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-x')).toEqual([{ kind: 'deleted', binary: false }])
    expect(kinds('--- a/g.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-x')).toEqual([{ kind: 'deleted', binary: false }])
    expect(kinds('--- a/m.ts\n+++ b/m.ts\n@@ -1 +1 @@\n-x\n+y')).toEqual([{ kind: 'modified', binary: false }])
    expect(kinds('diff --git a/logo.png b/logo.png\nindex 3333333..4444444 100644\nBinary files a/logo.png and b/logo.png differ')).toEqual([{ kind: 'modified', binary: true }])
    expect(kinds('diff --git a/logo.png b/logo.png\nnew file mode 100644\nindex 0000000..4444444\nBinary files /dev/null and b/logo.png differ')).toEqual([{ kind: 'added', binary: true }])
    // `git diff --binary`: the payload under `GIT binary patch` is neither
    // content nor something a plain body prints.
    const gitBinary = 'diff --git a/logo.png b/logo.png\nindex 3333333..4444444 100644\nGIT binary patch\nliteral 12\nzcmV+;0000\n\nliteral 10\nzcmV+;00\n'
    expect(kinds(gitBinary)).toEqual([{ kind: 'modified', binary: true }])
    expect(plainPatchHunks(gitBinary)).toEqual([])
    // A 100% rename and a mode-only change are changes to a file that is still there.
    expect(kinds('diff --git a/x.ts b/y.ts\nsimilarity index 100%\nrename from x.ts\nrename to y.ts')).toEqual([{ kind: 'modified', binary: false }])
    expect(kinds('diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755')).toEqual([{ kind: 'modified', binary: false }])
  })
})

/** The plain body under a header row the caller draws: the row already says
 *  which file, which rename, which mode change and how many lines, so the
 *  body prints the hunks' content and nothing the reader has to skip. */
describe('plainPatchHunks (the plain body under a caller-drawn row)', () => {
  it('drops the metadata block and the hunk headers, one string per hunk, and filters nothing after the first hunk header', () => {
    const diff = `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,2 +1,2 @@
 keep
-old
+new
\\ No newline at end of file
@@ -10,2 +10,3 @@
 later
+added`
    expect(plainPatchHunks(diff)).toEqual([' keep\n-old\n+new\n\\ No newline at end of file', ' later\n+added'])
  })

  /** Position, not shape: after the first `@@` a line is content whatever it
   *  starts with — a removed YAML rule (`----`), a removed `-- note`
   *  (`--- note`), an added `++i` (`+++i`), a template's ` @@`. */
  it('keeps header-shaped content inside a hunk, under a numeric header and under a bare one', () => {
    const numeric = `--- a/doc.md
+++ b/doc.md
@@ -1,4 +1,4 @@
 title: x
----
+--- rule
-i++
++++i
 @@ template @@`
    expect(plainPatchHunks(numeric)).toEqual([' title: x\n----\n+--- rule\n-i++\n++++i\n @@ template @@'])
    const bare = `--- a/q.sql
+++ b/q.sql
@@ selection @@
-SELECT 1;
--- note
+++ more
 SELECT 2;`
    expect(plainPatchHunks(bare)).toEqual(['-SELECT 1;\n--- note\n+++ more\n SELECT 2;'])
  })

  it('drops the similarity and rename lines the row restates', () => {
    const diff = `diff --git a/src/old.ts b/src/new.ts
similarity index 80%
rename from src/old.ts
rename to src/new.ts
--- a/src/old.ts
+++ b/src/new.ts
@@ -1,2 +1,3 @@
 export function f() {
+  // one positional walk
   return 1`
    expect(plainPatchHunks(diff)).toEqual([' export function f() {\n+  // one positional walk\n   return 1'])
    // A 100% rename has nothing left to print: the row says it all.
    expect(plainPatchHunks('diff --git a/x.ts b/y.ts\nsimilarity index 100%\nrename from x.ts\nrename to y.ts')).toEqual([])
  })

  /** An entry with no hunk has no content to print, and the row states what
   *  kind of change it is — a mode change, a binary change, an added file — so
   *  its body is empty: the row alone, as when Pierre draws it. A metadata line
   *  the row has no word for still prints, so nothing is hidden. */
  it('prints nothing for a hunk-less entry whose change the row states, and keeps a line it cannot', () => {
    expect(plainPatchHunks('diff --git a/bin/run.sh b/bin/run.sh\nold mode 100644\nnew mode 100755')).toEqual([])
    expect(plainPatchHunks('diff --git a/logo.png b/logo.png\nindex 3333333..4444444 100644\nBinary files a/logo.png and b/logo.png differ'))
      .toEqual([])
    expect(plainPatchHunks('diff --git a/empty b/empty\nnew file mode 100644\nindex 0000000..e69de29')).toEqual([])
    expect(plainPatchHunks('diff --git a/gone b/gone\ndeleted file mode 100644\nindex e69de29..0000000')).toEqual([])
    expect(plainPatchHunks('diff --git a/a.ts b/a.ts\ndissimilarity index 95%\nindex 1111111..2222222 100644')).toEqual(['dissimilarity index 95%'])
  })

  /** Under a hunk the metadata block is the row's business, not the body's: an
   *  added file's `new file mode 100644` must not print as the first line of
   *  its body, directly above the `+` lines that are the change, and a chmod
   *  beside an edit is stated by the row's mode note. */
  it('prints the hunks only for an entry that has one', () => {
    const added = `diff --git a/src/new.ts b/src/new.ts
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/src/new.ts
@@ -0,0 +1,2 @@
+export const x = 1
+export const y = 2`
    expect(plainPatchHunks(added)).toEqual(['+export const x = 1\n+export const y = 2'])
    const chmodAndEdit = `diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
--- a/run.sh
+++ b/run.sh
@@ -1 +1 @@
-echo one
+echo two`
    expect(plainPatchHunks(chmodAndEdit)).toEqual(['-echo one\n+echo two'])
  })

  it('keeps header-shaped content: a removed `-- note` is a deleted line, not a header', () => {
    const diff = `--- a/q.sql
+++ b/q.sql
@@ -1,2 +1,1 @@
-SELECT 1;
--- note
 SELECT 2;`
    expect(plainPatchHunks(diff)).toEqual(['-SELECT 1;\n--- note\n SELECT 2;'])
  })

  it('reads a fence with no hunk header by the prefix rule: `+`/`-` lines stay, `---`/`+++` go', () => {
    expect(plainPatchHunks('--- a/x.ts\n+++ b/x.ts\n-old\n+new')).toEqual(['-old\n+new'])
    expect(plainPatchHunks('-old\n+new\n+newer')).toEqual(['-old\n+new\n+newer'])
    expect(plainPatchHunks('')).toEqual([])
  })
})

describe('changedLineSpan (bounded change locality)', () => {
  const lines = (arr: string[]) => arr
  it('returns null for identical content', () => {
    expect(changedLineSpan(['a', 'b', 'c'], ['a', 'b', 'c'])).toBeNull()
  })

  it('finds a single deep edit as a one-line span on both sides', () => {
    const before = Array.from({ length: 1000 }, (_, i) => `L${i}`)
    const after = [...before]
    after[869] = 'L869 changed'
    expect(changedLineSpan(before, after)).toEqual({ oldStart: 869, oldEnd: 870, newStart: 869, newEnd: 870 })
  })

  it('covers scattered edits with one outer span', () => {
    const before = lines(['a', 'b', 'c', 'd', 'e'])
    const after = lines(['a', 'B', 'c', 'D', 'e'])
    // First diff at index 1, last diff at index 3 -> [1,4) on both sides.
    expect(changedLineSpan(before, after)).toEqual({ oldStart: 1, oldEnd: 4, newStart: 1, newEnd: 4 })
  })

  it('handles a pure insertion (empty removed range on the old side)', () => {
    const before = lines(['a', 'b', 'c'])
    const after = lines(['a', 'x', 'y', 'b', 'c'])
    const span = changedLineSpan(before, after)!
    // Common prefix 'a' (1), common suffix 'b','c' (2): old range is empty, new range is the two inserts.
    expect(span.oldStart).toBe(1)
    expect(span.oldEnd).toBe(1)
    expect(span.newStart).toBe(1)
    expect(span.newEnd).toBe(3)
  })

  it('handles append at end and prepend at start', () => {
    expect(changedLineSpan(['a', 'b'], ['a', 'b', 'c'])).toEqual({ oldStart: 2, oldEnd: 2, newStart: 2, newEnd: 3 })
    expect(changedLineSpan(['b', 'c'], ['a', 'b', 'c'])).toEqual({ oldStart: 0, oldEnd: 0, newStart: 0, newEnd: 1 })
  })

  it('treats a whole-file replacement as a full-range span', () => {
    expect(changedLineSpan(['a', 'b'], ['x', 'y', 'z'])).toEqual({ oldStart: 0, oldEnd: 2, newStart: 0, newEnd: 3 })
  })
})
