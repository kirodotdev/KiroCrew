import { safeSetItem } from '../../utils/safeStorage'
import { STORAGE_KEY } from './constants'
import { fmtBytes, fmtDateTimeNumeric } from '../../i18n/format'
import { hasCommandModifier } from '../../utils/commandModifier'

export const extOf = (p: string) => {
  const slash = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  const base = p.slice(slash + 1)
  const i = base.lastIndexOf('.')
  if (i <= 0) return ''
  return base.slice(i).toLowerCase()
}

export const basename = (p: string) => {
  const s = p.replace(/\/+$/, '')
  const i = s.lastIndexOf('/')
  return i < 0 ? s : s.slice(i + 1)
}

export const dirname = (p: string) => {
  const s = p.replace(/\/+$/, '')
  const i = s.lastIndexOf('/')
  if (i <= 0) return '/'
  return s.slice(0, i)
}

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

export function isSensitivePath(path: string): boolean {
  return SENSITIVE_PATTERNS.some(p => p.test(path))
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
