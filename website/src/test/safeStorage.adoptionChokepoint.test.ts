import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

/**
 * A file that imports `safeStorage` writes through it.
 *
 * `src/utils/safeStorage.ts` exists because the dashboard's per-origin quota
 * fills up (`vc_heights_*` caches, `mc-paste-store-v1`). Once it does, the next
 * `setItem` throws `QuotaExceededError`; on the websocket `onmessage` -> dispatch
 * -> re-render path that escapes a React ErrorBoundary and white-screens the app.
 * `safeSetItem` reclaims one disposable tier and retries, so the write survives.
 *
 * The failure mode this guards is a half-finished migration: a file adopts the
 * helper for one key and leaves a neighbouring raw write, which is exactly the
 * shape `dashboardSlice.ts` had (safeSetItem for `mc-unread-slots` beside a raw
 * `mc-unread-shared`). A reviewer cannot see the omission; a new write is added
 * by someone who has never read this file. So the rule is enforced on the source
 * rather than on attention.
 *
 * Scope is deliberately narrow: only files that ALREADY import `safeStorage`.
 * A file with no import is a different (larger) migration and is not policed
 * here. Reads and `removeItem` are out of scope — this is about the write that
 * throws.
 *
 * There is no exemption marker. Every file in scope writes through the helper
 * today, so an escape hatch would be a convention with no consumer; if a
 * genuine exemption ever appears, it can add one alongside its reason.
 */

const SRC = join(process.cwd(), 'src')

/** Every `.ts`/`.tsx` under `src/`, excluding tests and the helper itself. */
function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full))
      continue
    }
    if (!/\.tsx?$/.test(name)) continue
    if (/\.test\.tsx?$/.test(name)) continue
    out.push(full)
  }
  return out
}

const IMPORTS_HELPER = /from\s+['"][^'"]*safeStorage['"]/
/** A raw write to Web Storage. `window.`/`globalThis.` prefixes are matched. */
const RAW_WRITE = /(?:window\.|globalThis\.)?(?:local|session)Storage\.setItem\(/

describe('safeStorage importers write through safeStorage', () => {
  const files = sourceFiles(SRC)

  it('finds the imports and raw writes it is meant to police', () => {
    // A regex that silently stopped matching would make the case below pass
    // vacuously, so pin both halves against the shapes they must catch.
    expect(IMPORTS_HELPER.test(`import { safeSetItem } from '../utils/safeStorage'`)).toBe(true)
    expect(IMPORTS_HELPER.test(`import { safeSetItem } from './safeStorage'`)).toBe(true)
    expect(RAW_WRITE.test(`localStorage.setItem('k', 'v')`)).toBe(true)
    expect(RAW_WRITE.test(`window.sessionStorage.setItem('k', 'v')`)).toBe(true)
    expect(RAW_WRITE.test(`localStorage.getItem('k')`)).toBe(false)
    expect(RAW_WRITE.test(`safeSetItem('k', 'v')`)).toBe(false)
    // And that the scan actually reaches source files.
    expect(files.length).toBeGreaterThan(100)
  })

  it('has no raw Web Storage write in a file that imports safeStorage', () => {
    const offenders: string[] = []
    for (const file of files) {
      const text = readFileSync(file, 'utf8')
      if (!IMPORTS_HELPER.test(text)) continue
      text.split('\n').forEach((line, i) => {
        if (RAW_WRITE.test(line)) {
          offenders.push(`${relative(process.cwd(), file)}:${i + 1}: ${line.trim()}`)
        }
      })
    }
    expect(offenders).toEqual([])
  })
})
