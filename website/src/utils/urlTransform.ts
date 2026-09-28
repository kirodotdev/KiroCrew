import { defaultUrlTransform } from 'react-markdown'

export const ALLOWED_PROTOCOLS = new Set([
  'vscode:',
  'vscode-insiders:',
])

/** Windows drive-letter absolute path (`C:/…` or `C:\…`). THE single copy of
 *  this predicate — it decides both which image `src` values bypass
 *  `defaultUrlTransform` (below) and which are treated as local file reads
 *  (ImgWithFallback), so the two decisions can never drift apart.
 *
 *  Deliberately EXCLUDES backslash UNC (`\\host\…`): a UNC path names a HOST,
 *  and letting attacker-authored markdown route one to `/api/file-raw` would
 *  hand Windows an outbound SMB authentication probe. Legitimate UNC upload
 *  paths never reach the renderer in backslash form — mdImageDest normalizes
 *  them to `//host/share/…`, which flows through `defaultUrlTransform` as a
 *  scheme-less relative URL and is validated against the gateway's trusted
 *  attachment roots server-side before any filesystem resolution.
 *
 *  Anchored and separator-required so real URI schemes never match — every
 *  registered scheme is 2+ characters (`js:`), and a single-letter scheme
 *  without a following separator (`c:foo`) is still rejected. */
export const WINDOWS_ABS_PATH_RE = /^[A-Za-z]:[\\/]/

/** Recover the on-disk path from a markdown-sourced image `src`.
 *
 *  micromark percent-encodes markdown destinations (a space in an `<…>`
 *  destination arrives as `%20`), and our producer (`mdImageDest` in
 *  fileTokens.ts) escapes a literal `%` to `%25` — so one decode is the exact
 *  inverse for every destination this app produces. Two fail-safe rails keep a
 *  hand-authored src from becoming an attack or a crash:
 *  - a malformed sequence (`%zz`) keeps the raw form instead of throwing;
 *  - a decode that produces control characters (`%00` → NUL would make the
 *    file-raw backend's realpath raise) keeps the raw form. */
export function decodeLocalPath(src: string): string {
  let decoded: string
  try { decoded = decodeURIComponent(src) } catch { return src }
  if (/[\u0000-\u001f]/.test(decoded)) return src
  return decoded
}

export function urlTransform(url: string, key?: string): string {
  // A local image on Windows is an absolute drive path (`C:/…/uploads/x.png`).
  // `defaultUrlTransform` parses the drive letter as an unknown `c:` scheme and
  // returns '' — the <img> then renders as nothing (issue #3497). Pass the path
  // through for image `src` only: ImgWithFallback routes local paths to the
  // same-origin `/api/file-raw?path=…` endpoint, so the raw drive path never
  // reaches the DOM. `href` and other keys keep the default strict transform
  // (a link to a bare drive path has no meaning in the browser), and the shape
  // cannot express `javascript:`/`data:` payloads (single letter + separator).
  if (key === 'src' && WINDOWS_ABS_PATH_RE.test(url)) return url
  try {
    const u = new URL(url)
    if (ALLOWED_PROTOCOLS.has(u.protocol) && u.href.length > u.protocol.length + '//'.length)
      return u.href
  } catch {}
  return defaultUrlTransform(url)
}
