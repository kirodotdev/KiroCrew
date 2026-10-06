/** Separator-aware file-name helpers for gateway paths shown in the UI.
 *
 *  A Windows gateway reports native paths (`C:\repo\src\a.ts`,
 *  `\\host\share\a.ts`), so splitting on `/` alone shows the whole path where a
 *  file name belongs. Splitting on `\` everywhere is wrong too: on POSIX a
 *  backslash is a legal file-name character, so `/tmp/we\ird.md` is ONE name.
 *  The rule here is the one `breadcrumbSegments` (MarkdownPanel.tsx) uses: `\`
 *  is a separator only when the path itself is Windows-shaped -- drive-rooted
 *  (`C:\`, `C:/`) or a backslash UNC path (`\\host\`).
 *
 *  This module is display-only: it decides which characters to SHOW, never
 *  which paths may be read. The security-relevant predicate stays
 *  `WINDOWS_ABS_PATH_RE` in urlTransform.ts, which deliberately excludes UNC.
 *  The regex below is the one definition of that shape: fileTokens.ts imports
 *  it for `normalizeWindowsPath` rather than keeping its own copy. */

/** Drive-rooted (`C:\…`, `C:/…`) or backslash-UNC (`\\host\…`) path. */
export const WINDOWS_SHAPED_PATH_RE = /^(?:[A-Za-z]:|\\\\[^\\/]+)[\\/]/

/** Whether `\` separates segments in `p` (see the module comment). */
export function backslashIsSeparator(p: string): boolean {
  return WINDOWS_SHAPED_PATH_RE.test(p)
}

/** The text after the last separator of `p`; `''` when `p` ends with one.
 *  For a POSIX path this is exactly `p.split('/').pop()`. */
export function pathBasename(p: string): string {
  const cut = backslashIsSeparator(p)
    ? Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
    : p.lastIndexOf('/')
  return p.slice(cut + 1)
}

/** `p` without its trailing separators. For a POSIX path this is exactly
 *  `p.replace(/\/+$/, '')`; a Windows-shaped path also drops trailing `\`. */
export function stripTrailingSeparators(p: string): string {
  return p.replace(backslashIsSeparator(p) ? /[\\/]+$/ : /\/+$/, '')
}
