import { describe, it, expect } from 'vitest'
import { parsePatchFiles } from '@pierre/diffs'
import { normalizePatchHunks } from '../pierre/PierreImpl'

const SECTION_HEADER_PATCH = `--- a/src/index.css
+++ b/src/index.css
@@ .pierre-surface @@
   --diffs-font-size:13px;
+  --diffs-light-bg:var(--bg-elevated);
+  --diffs-dark-bg:var(--bg-elevated);
`

const BARE_HEADER_PATCH = `--- a/src/main.tsx
+++ b/src/main.tsx
@@
+const idle = (cb) => setTimeout(cb, 2000)
+idle(() => {})
`

const VALID_PATCH = `--- a/x.py
+++ b/x.py
@@ -191,1 +191,2 @@
         date = last_sat
+    env = "prod"
`

/** Syntactically valid header whose counts overshoot the body (9/11 declared,
 *  1/2 actual) — the shape a hand-written patch usually gets wrong. */
const WRONG_COUNT_PATCH = `--- a/x.py
+++ b/x.py
@@ -191,9 +191,11 @@
         date = last_sat
+    env = "prod"
`

/** No hunk header at all: Pierre parses this as a zero-hunk PURE RENAME and
 *  renders a header with +0 −0 and no rows. */
const NO_HEADER_PATCH = `--- a/src/index.css
+++ b/src/index.css
   --trees-font-size-override:12.5px;
+  --trees-padding-inline-override:0px;
   --trees-git-added-color-override:var(--diff-add-text);
`

describe('normalizePatchHunks', () => {
  it('rewrites a section-text hunk header into a parseable numeric one', () => {
    const norm = normalizePatchHunks(SECTION_HEADER_PATCH)
    expect(norm).toContain('@@ -1,1 +1,3 @@ .pierre-surface')
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files).toHaveLength(1)
    expect(files[0].hunks.length).toBeGreaterThan(0)
  })

  it('rewrites a bare @@ header', () => {
    const norm = normalizePatchHunks(BARE_HEADER_PATCH)
    expect(norm).toContain('@@ -1,0 +1,2 @@')
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files[0].hunks.length).toBeGreaterThan(0)
  })

  it('returns valid patches byte-identical', () => {
    expect(normalizePatchHunks(VALID_PATCH)).toBe(VALID_PATCH)
  })

  it('corrects declared counts that overshoot the body, keeping the start lines', () => {
    const norm = normalizePatchHunks(WRONG_COUNT_PATCH)
    expect(norm).toContain('@@ -191,1 +191,2 @@')
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files[0].hunks).toHaveLength(1)
  })

  it('synthesizes a hunk header when the file section has none', () => {
    const norm = normalizePatchHunks(NO_HEADER_PATCH)
    expect(norm).toContain('@@ -1,2 +1,3 @@')
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files).toHaveLength(1)
    expect(files[0].hunks).toHaveLength(1)
    // The zero-hunk parse read this as a rename of a file nobody renamed.
    expect(files[0].type).not.toBe('rename-pure')
  })

  it('pads unprefixed context lines so the parser accepts them', () => {
    // Explanatory diffs routinely omit the leading space on context lines, and
    // an elision marker never has one; Pierre rejects such a line outright.
    const unprefixed = `--- a/rail.tsx
+++ b/rail.tsx
@@ bounds @@
const RAIL_MIN_W = 200
-const RAIL_MAX_W = 520
+const RAIL_MAX_W = 560
...
`
    const norm = normalizePatchHunks(unprefixed)
    expect(norm).toContain('\n const RAIL_MIN_W = 200')
    expect(norm).toContain('\n ...')
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files[0].hunks).toHaveLength(1)
  })

  it('leaves a hunk body line that merely looks like a file header alone', () => {
    // `+++ x` here is an ADDITION of a line starting `++ `, not a file header;
    // synthesizing a hunk at that point would corrupt a valid patch.
    const bodyLooksLikeHeader = `--- a/c.md
+++ b/c.md
@@ -1,1 +1,2 @@
 keep
+++ nested marker
`
    expect(normalizePatchHunks(bodyLooksLikeHeader)).toBe(bodyLooksLikeHeader)
  })

  it('keeps a PAIRED body deletion/addition that reads exactly like a file header', () => {
    // Deleting a line that starts `-- ` and adding one that starts `++ ` puts
    // `--- x` directly above `+++ y` INSIDE the hunk. Pairing alone cannot tell
    // that from a file header, and consuming it as one truncates the hunk: the
    // rows below it disappear from the render.
    const paired = `--- a/notes.md
+++ b/notes.md
@@ -1,3 +1,3 @@
 keep
-- foo/bar
++ baz/qux
 tail
`
    const norm = normalizePatchHunks(paired)
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files).toHaveLength(1)
    expect(files[0].hunks).toHaveLength(1)
    const rendered = norm.split('\n')
    expect(rendered).toContain('-- foo/bar')
    expect(rendered).toContain('++ baz/qux')
    expect(rendered).toContain(' tail')
    // Counts cover the whole body, so the hunk is not cut at the fake header.
    expect(norm).toContain('@@ -1,3 +1,3 @@')
  })

  it('numbers successive synthesized hunks with running line counters', () => {
    const multi = `${SECTION_HEADER_PATCH}@@ another.section @@
-  old:1;
+  new:2;
`
    const norm = normalizePatchHunks(multi)
    expect(norm).toContain('@@ -2,1 +4,1 @@ another.section')
    const files = parsePatchFiles(norm).flatMap(p => p.files)
    expect(files[0].hunks).toHaveLength(2)
  })
})
