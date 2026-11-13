/**
 * The campaign status labels hold catalog keys, and every key resolves.
 *
 * `STATE_LABEL_KEY` maps each backend status to a catalog key, like the core tables
 * `src/test/labelKeyTables.test.ts` covers. It is checked beside the page so the
 * app's UI can leave the repository with its own test. Both halves are pinned: the
 * key exists in English, and it is translated rather than left in English.
 */
import { describe, it, expect } from 'vitest'

import { CATALOGS } from '../../i18n/catalogs'
import { STATE_LABEL_KEY } from './ResearchLabPage'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const en = flatten((CATALOGS.en as { translation: unknown }).translation)
const zh = flatten((CATALOGS['zh-CN'] as { translation: unknown }).translation)

describe('STATE_LABEL_KEY holds catalog keys', () => {
  const keys = Object.values(STATE_LABEL_KEY)

  it('every value is a key that exists in English', () => {
    expect(keys.filter(key => !(key in en))).toEqual([])
  })

  it('every key is translated, not left in English', () => {
    expect(keys.filter(key => zh[key] === en[key])).toEqual([])
  })
})
