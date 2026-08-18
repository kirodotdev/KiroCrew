/**
 * The framed send-failed string is core-owned — a drift guard for issue #4240.
 *
 * ## The defect this guard closes
 *
 * The refused-send error rows (#4198) framed the server reason with
 * `apps.designTweak.status.send_failed` because it was the only framed variant
 * in the catalog. That made core chrome depend on an APP's catalog namespace:
 * an app-side string edit could silently reword core error rows. The entry is
 * now promoted to `pages.chatPage.send_failed_with_error` (a sibling of the
 * unframed `pages.chatPage.send_failed` that ChatPage already uses), and the
 * app key is deleted.
 *
 * Two invariants, each of which failed silently before:
 *
 * 1. The shared key exists in EVERY locale catalog and carries the `{{error}}`
 *    placeholder. i18next renders a missing key as the key string itself, so a
 *    locale that lost the entry would ship
 *    `pages.chatPage.send_failed_with_error` as visible UI text.
 * 2. No source file references the retired app-namespace key. A call site that
 *    reaches for `apps.designTweak.status.send_failed` re-creates the
 *    core-depends-on-app coupling this move removed — and, now that the entry
 *    is deleted, renders the raw key.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './index'

const SHARED_KEY = 'pages.chatPage.send_failed_with_error'
const RETIRED_KEY = 'apps.designTweak.status.send_failed'

/** Resolve a dotted key against a nested catalog object. */
function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

/** All source files under `src/`, the same walk shape as deadKeys.test.ts. */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      if (name === 'node_modules' || name === 'locales') continue
      sourceFiles(p, out)
    } else if (/\.(tsx?|mjs|jsx?)$/.test(name)) {
      out.push(p)
    }
  }
  return out
}

describe('framed send_failed lives in the shared namespace (#4240)', () => {
  it('every locale catalog carries the shared key with the {{error}} placeholder', () => {
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      const value = resolve(catalog.translation, SHARED_KEY)
      expect(typeof value, `${lang} is missing ${SHARED_KEY}`).toBe('string')
      expect(value as string, `${lang} lost the {{error}} placeholder`).toContain('{{error}}')
    }
  })

  it('no locale catalog still carries the retired designTweak key', () => {
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      const value = resolve(catalog.translation, RETIRED_KEY)
      expect(value, `${lang} still carries ${RETIRED_KEY}`).toBeUndefined()
    }
  })

  it('no source file references the retired app-namespace key', () => {
    const src = join(__dirname, '..')
    const offenders = sourceFiles(src).filter(p => {
      // This guard file names the retired key on purpose.
      if (p.endsWith('sendFailedSharedKey.test.ts')) return false
      return readFileSync(p, 'utf8').includes(RETIRED_KEY)
    })
    expect(offenders, `core must not reference ${RETIRED_KEY}`).toEqual([])
  })
})
