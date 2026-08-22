/**
 * Wraps a bare patch body in the `diff --git` / `---` / `+++` headers Pierre
 * needs to identify a file. The text is git's wire format, parsed by Pierre --
 * never read as words -- which is why it lives here rather than in the panel
 * that renders it (see this path in `eslint.i18n.config.js`).
 */
export function withUnifiedPatchHeaders(path: string, patch: string): string {
  return `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n${patch}`
}

/** Placeholder path for a patch that arrived with no file section at all, so it
 *  can satisfy Pierre's named-header requirement. Never a real file, and never
 *  shown: surfaces that display Pierre's file header hide it when the patch
 *  named no file of its own. */
const PATCH_SNIPPET_NAME = 'snippet'

/** The `---`/`+++` pair alone, for a caller assembling a patch line by line. */
export function snippetFileHeaderLines(): [string, string] {
  return [`--- a/${PATCH_SNIPPET_NAME}`, `+++ b/${PATCH_SNIPPET_NAME}`]
}

/** Hunk-body extents derived from UNAMBIGUOUS delimiters only (`@@`, `diff `).
 *  Header detection consults this, so it can ask "am I inside a hunk body?"
 *  without depending on itself. */
export function markHunkBodies(lines: string[]): boolean[] {
  const body = new Array<boolean>(lines.length).fill(false)
  for (let h = 0; h < lines.length; h++) {
    if (!lines[h].startsWith('@@')) continue
    for (let j = h + 1; j < lines.length; j++) {
      if (lines[j].startsWith('@@') || lines[j].startsWith('diff ')) break
      body[j] = true
    }
  }
  return body
}

/** True when `line` is a `--- `/`+++ ` marker carrying a file name. An empty name
 *  (`--- ` alone) is not one: Pierre parses such a pair to zero files, so treating
 *  it as a file section would suppress the repair the patch needs. */
function namesFile(line: string, prefix: '--- ' | '+++ '): boolean {
  return line.startsWith(prefix) && line.slice(prefix.length).trim() !== ''
}

/** True when `--- `/`+++ ` at `i` is a real file-header pair rather than a hunk
 *  body line deleting `-- x` / adding `++ x`.
 *
 *  Inside a hunk body those two are indistinguishable by shape — a deletion of
 *  `-- foo/bar` IS the text `--- foo/bar` — so a pair there is content unless it
 *  announces itself the way a real file section does: `diff ` above it, or a `@@`
 *  hunk header immediately below. Known limit: a second file section that is BOTH
 *  headerless and un-announced while a previous hunk is open reads as content;
 *  git always emits `diff --git`, so that shape is not produced. */
export function isFileHeaderAt(lines: string[], hunkBody: boolean[], i: number): boolean {
  const minus = namesFile(lines[i], '--- ') ? i : namesFile(lines[i], '+++ ') ? i - 1 : -1
  if (minus < 0) return false
  if (!namesFile(lines[minus] ?? '', '--- ')) return false
  if (!namesFile(lines[minus + 1] ?? '', '+++ ')) return false
  if (!hunkBody[minus]) return true
  return (lines[minus - 1] ?? '').startsWith('diff ') || (lines[minus + 2] ?? '').startsWith('@@')
}

/** Whether the patch carries a file-header pair of its own — the one rule both
 *  the repair pass (which synthesizes a pair only when none exists) and the
 *  surfaces that show Pierre's file header (which hide it when the patch named
 *  no file) ask, so the two cannot answer differently for the same patch. */
export function patchNamesAFile(patch: string): boolean {
  const lines = patch.split('\n')
  const hunkBody = markHunkBodies(lines)
  return lines.some((_, i) => isFileHeaderAt(lines, hunkBody, i))
}
