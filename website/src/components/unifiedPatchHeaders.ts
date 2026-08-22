/**
 * Wraps a bare patch body in the `diff --git` / `---` / `+++` headers Pierre
 * needs to identify a file. The text is git's wire format, parsed by Pierre --
 * never read as words -- which is why it lives here rather than in the panel
 * that renders it (see this path in `eslint.i18n.config.js`).
 */

/** A file's change type in git's own vocabulary.
 *
 *  Pierre derives the type -- and therefore the icon on its stock file header --
 *  from the `new file mode` / `deleted file mode` lines, NOT from the `/dev/null`
 *  side (which only tells its parser which side carries the name). So a provider
 *  that reports the status separately from the patch body has to spell BOTH into
 *  the headers, or every file renders with the plain-modification icon. */
export type PatchChange = 'change' | 'new' | 'deleted'

/** git's own name for a side that does not exist. */
const ABSENT_SIDE = '/dev/null'

/** Mode git writes for a regular non-executable file. The real mode is not
 *  recoverable from a provider's per-file patch body, and nothing we render
 *  displays it -- it is here because the mode LINE is what carries the type. */
const REGULAR_FILE_MODE = '100644'

/** Characters that would let a filename break out of its header line (newline
 *  foremost) or be misread as quoting. Everything else passes through bare. */
const NEEDS_QUOTING = /[\n\r\t"\\]/

/** C-quote a header name the way git itself writes unusual paths -- wrapped in
 *  double quotes with backslash escapes -- so a crafted filename cannot inject
 *  header lines and silently drop that file's diff from the review surface.
 *  Ordinary paths come back untouched, keeping the fast path byte-identical. */
function quoteName(name: string): string {
  if (!NEEDS_QUOTING.test(name)) return name
  return `"${name
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')}"`
}

export function withUnifiedPatchHeaders(
  path: string,
  patch: string,
  change: PatchChange = 'change',
): string {
  const modeLine = change === 'new'
    ? `new file mode ${REGULAR_FILE_MODE}\n`
    : change === 'deleted'
      ? `deleted file mode ${REGULAR_FILE_MODE}\n`
      : ''
  const aSide = quoteName(`a/${path}`)
  const bSide = quoteName(`b/${path}`)
  const oldSide = change === 'new' ? ABSENT_SIDE : aSide
  const newSide = change === 'deleted' ? ABSENT_SIDE : bSide
  return `diff --git ${aSide} ${bSide}\n${modeLine}--- ${oldSide}\n+++ ${newSide}\n${patch}`
}

/** Map a provider's file-status token onto a `PatchChange`.
 *
 *  GitHub reports `added` / `removed` / `modified` / `renamed` / `copied` /
 *  `changed` / `unchanged`; GitLab's payload is normalized to the same set
 *  server-side. Only the two that change the icon are distinguished -- a rename
 *  needs the PREVIOUS path, which a per-file patch payload does not carry, so it
 *  is deliberately left as a plain change rather than guessed at. */
export function patchChangeForStatus(status: string): PatchChange {
  const token = status.toLowerCase()
  if (token === 'added' || token === 'new') return 'new'
  if (token === 'removed' || token === 'deleted') return 'deleted'
  return 'change'
}
