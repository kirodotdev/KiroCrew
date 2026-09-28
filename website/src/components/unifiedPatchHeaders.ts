/**
 * Wraps a bare patch body in the `diff --git` / `---` / `+++` headers Pierre
 * needs to identify a file. The text is git's wire format, parsed by Pierre --
 * never read as words -- which is why it lives here rather than in the panel
 * that renders it (see this path in `eslint.i18n.config.js`).
 */
export function withUnifiedPatchHeaders(path: string, patch: string): string {
  return `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n${patch}`
}
