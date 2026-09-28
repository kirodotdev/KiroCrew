/**
 * The chat diff block draws one row per section `splitPatchSections` emits and
 * hands each section's text to its own `PierrePatch`. The rows line up with the
 * bodies only while the block's walk and Pierre's parser agree on where a file
 * begins: an under-cut section would put two Pierre bodies under one row with
 * one file's name and counts, an over-cut one a headless half-file under a row
 * of its own. This pins the agreement over the corpus `diffStats.test.ts`
 * carries — git and difflib shapes, additions, deletions, renames with and
 * without hunks, binary and mode-only entries, header-shaped content inside a
 * hunk, miscounted hunks in both directions, a copy — by parsing every emitted
 * section the way `PierreImpl` does (`parsePatchFiles` over
 * `normalizePatchHunks`, which repairs stale counts from the body) and
 * requiring exactly one file, named as the row is.
 *
 * Its own file, not a case in `diffStats.test.ts`: that suite deliberately
 * imports nothing but the pure helpers, and `@pierre/diffs` is the diff
 * runtime it keeps out.
 */
import { describe, it, expect } from 'vitest'
import { parsePatchFiles } from '@pierre/diffs'
import { splitPatchSections } from '../utils/diffLineCounts'
import { normalizePatchHunks } from '../pierre/PierreImpl'

const CORPUS: Record<string, string> = {
  'git, two modified files': `diff --git a/one.ts b/one.ts
index 1111111..2222222 100644
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
+new two
`,
  'difflib, no preamble': `--- a.py
+++ a.py
@@ -1 +1 @@
-x = 1
+x = 2
--- b.py
+++ b.py
@@ -1,2 +1 @@
 keep
-gone`,
  'an addition, a deletion and a rename, timestamped headers': `--- /dev/null
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
+y`,
  'header-shaped content inside a hunk': `--- a/doc.md
+++ b/doc.md
@@ -1,4 +1,4 @@
 title: x
----
+--- rule
-i++
++++i
 end`,
  'a lone `--- ` line past a miscounted hunk': `--- a/q.sql
+++ b/q.sql
@@ -1,1 +1,1 @@
-SELECT 1;
--- note
+SELECT 2;`,
  'a hunk whose counts overshoot its body, then a second file': `--- a/x.ts
+++ b/x.ts
@@ -1,5 +1,5 @@
-a
+b
--- a/y.ts
+++ b/y.ts
@@ -1,5 +1,5 @@
-c
+d`,
  'a hunk whose counts undershoot its body, then header-shaped content': `--- a/q.sql
+++ b/q.sql
@@ -1,1 +1,1 @@
-SELECT 1;
+SELECT 2;
--- foo
+++ bar
 SELECT 3;`,
  'a 100% copy beside a modification': `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-const a = 1
+const a = 2
diff --git a/src/a.ts b/src/b.ts
similarity index 100%
copy from src/a.ts
copy to src/b.ts`,
  'hunk-less entries: 100% rename, binary, mode-only, beside a modification': `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-const a = 1
+const a = 2
diff --git a/src/old.ts b/src/new.ts
similarity index 100%
rename from src/old.ts
rename to src/new.ts
diff --git a/assets/logo.png b/assets/logo.png
index 3333333..4444444 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
diff --git a/bin/run.sh b/bin/run.sh
old mode 100644
new mode 100755`,
}

/** Pierre keeps git's `a/` / `b/` markers on the names it parses; the block
 *  strips them (`headerPath`), so compare the two without them. */
const unprefixed = (name: string | undefined) => (name == null ? null : name.replace(/^[ab]\//, ''))

describe('splitPatchSections agrees with Pierre on where a file begins', () => {
  for (const [label, diff] of Object.entries(CORPUS)) {
    it(`every section of "${label}" parses as exactly one Pierre file, named as its row is`, () => {
      const sections = splitPatchSections(diff)
      expect(sections.length).toBeGreaterThan(0)
      for (const section of sections) {
        // What Pierre parses: PierreImpl hands `parsePatchFiles` the section
        // through `normalizePatchHunks`, which repairs stale hunk counts from
        // the body before Pierre trusts them.
        const files = parsePatchFiles(normalizePatchHunks(section.text)).flatMap(p => p.files)
        expect(files, section.text).toHaveLength(1)
        // A deletion is named on the `---` side by both; everything else on the `+++`.
        const pierreName = unprefixed(files[0].name) ?? unprefixed(files[0].prevName)
        expect(pierreName).toBe(section.name)
      }
    })
  }
})
