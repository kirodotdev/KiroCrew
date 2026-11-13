import { safeSetItem } from '../../utils/safeStorage'
import { STORAGE_KEY } from './constants'
import { fmtBytes, fmtDateTimeNumeric } from '../../i18n/format'
import { hasCommandModifier } from '../../utils/commandModifier'
import { backslashIsSeparator, pathBasename, stripTrailingSeparators } from '../../utils/pathBasename'
import { parentDir } from './deepLink'

export const extOf = (p: string) => {
  const slash = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  const base = p.slice(slash + 1)
  const i = base.lastIndexOf('.')
  if (i <= 0) return ''
  return base.slice(i).toLowerCase()
}

export const basename = (p: string) => pathBasename(stripTrailingSeparators(p))

/** A UNC share root (`\\host\share`), the top of its own tree. */
const UNC_SHARE_ROOT_RE = /^\\\\[^\\/]+[\\/][^\\/]+[\\/]*$/

/** The folder that contains `p`, used to re-root a folder tab.
 *
 *  A Windows gateway prints paths with backslashes (`str(Path)` in the Files
 *  backend), so a Windows-shaped path (drive-rooted or backslash UNC, the
 *  `backslashIsSeparator` test) takes `parentDir`'s separator-aware cut. At a
 *  drive root (`C:\`) or a UNC share root (`\\host\share`) there is no parent,
 *  so the path itself comes back and re-rooting to it is a no-op. Every other
 *  path keeps the POSIX rule: only `/` separates, because on POSIX a backslash
 *  is a legal file-name character (`/tmp/we\ird` is one name). */
export const dirname = (p: string) => {
  if (backslashIsSeparator(p)) {
    if (UNC_SHARE_ROOT_RE.test(p)) return p
    return parentDir(p) ?? p
  }
  const s = p.replace(/\/+$/, '')
  const i = s.lastIndexOf('/')
  if (i <= 0) return '/'
  return s.slice(0, i)
}

/** Label for one `parentChain` crumb. The chain is built by splitting on `/`
 *  only, so its label splits on `/` only too: a Windows root stays one crumb
 *  showing the whole path, exactly as before #14581. Breadcrumb navigation for
 *  Windows roots is a separate change. */
export const crumbLabel = (p: string) => p.replace(/\/+$/, '').split('/').at(-1) ?? ''

export const parentChain = (p: string) => {
  const out: string[] = []
  let cur = p.replace(/\/+$/, '')
  while (cur && cur !== '/') {
    out.unshift(cur)
    const i = cur.lastIndexOf('/')
    cur = i <= 0 ? '/' : cur.slice(0, i)
  }
  out.unshift('/')
  return out
}

export const formatBytes = (n: number | null | undefined) => {
  if (n == null) return ''
  return fmtBytes(n)
}

export const formatTime = (sec: number | null | undefined) => {
  if (!sec) return ''
  // Convert here rather than handing `toDate` a bare number: a filesystem mtime
  // can legitimately be NEGATIVE (a pre-epoch file, `st_mtime = -1`), and the
  // seam treats `<= 0` as unparseable and renders an em dash. The cron and
  // session timestamps elsewhere come from `time.time()` and cannot be negative,
  // which is why they pass their seconds straight through.
  return fmtDateTimeNumeric(new Date(sec * 1000))
}

/**
 * The platform command modifier is held — Cmd on macOS, Ctrl elsewhere.
 * See `hasCommandModifier` for why Cmd+Ctrl together does not count.
 */
export const isShortcut = (e: KeyboardEvent) => hasCommandModifier(e)

const SENSITIVE_PATTERNS = [
  /\/\.ssh\//, /\/\.aws\//, /\/\.gnupg\//, /\/\.env$/, /\/\.env\./,
  /\/\.npmrc$/, /\/\.netrc$/, /\/credentials$/, /\/\.git-credentials$/,
  /\/\.docker\/config\.json$/, /\/\.kube\/config$/,
]

/**
 * Whether viewing this file warrants the screen-sharing warning.
 *
 * Matched against a slash-normalized copy, not the raw string. Paths reach the
 * UI exactly as the backend printed them — `str(p)` on a `pathlib.Path` in
 * `file_explorer/server.py` — so on Windows every separator is a backslash and
 * the `/`-anchored patterns above match nothing at all. `extOf` already splits
 * on both separators for the same reason; this is the rest of that contract.
 *
 * Normalizing rather than widening each pattern to `[/\\]` keeps one readable
 * pattern list and cannot drop a leading-separator anchor by hand. A POSIX file
 * whose NAME legitimately contains a backslash (`a\.ssh\k`) can now match, which
 * is the fail-safe direction here: the only consequence is an advisory banner
 * shown when it need not be, and this caller does nothing else with the verdict.
 */
export function isSensitivePath(path: string): boolean {
  const normalized = path.replace(/\\/g, '/')
  return SENSITIVE_PATTERNS.some(p => p.test(normalized))
}

export const loadState = () => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || !Array.isArray(parsed.folderTabs)) return null
    return parsed
  } catch {
    return null
  }
}

export const saveState = (state: unknown) => {
  try {
    safeSetItem(STORAGE_KEY, JSON.stringify(state))
  } catch { /* quota */ }
}
