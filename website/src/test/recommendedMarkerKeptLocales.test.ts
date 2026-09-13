import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const LOCALES = resolve(__dirname, '../i18n/locales')
const KEY = 'recommended_marker_kept'

function valueOf(file: string): string | null {
  const doc = JSON.parse(readFileSync(resolve(LOCALES, file), 'utf8')) as unknown
  const walk = (node: unknown): string | null => {
    if (node && typeof node === 'object') {
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
        if (k === KEY && typeof v === 'string') return v
        const hit = walk(v)
        if (hit !== null) return hit
      }
    }
    return null
  }
  return walk(doc)
}

const files = readdirSync(LOCALES).filter(f => f.endsWith('.json'))

describe('the guard-declined explainer survives translation', () => {
  it('reads the locale directory at all', () => {
    // Without this a directory rename would make every assertion below vacuous.
    expect(files.length).toBeGreaterThan(10)
  })

  it('says SEND in Korean, not "exclusively assigns"', () => {
    // 전속 ("exclusively assign") for 전송 ("send") shipped once here, and it lands on the
    // one sentence telling a Korean reader what a click dispatches.
    const ko = valueOf('ko.json')!
    expect(ko).toContain('전송')
    expect(ko).not.toContain('전속')
  })

  it('names the marker verbatim in every catalogue that carries the key', () => {
    // A localised marker describes a string the parser never emits. en-XA is exempt BY
    // DESIGN: pseudo-localisation accents every letter, marker included.
    const carrying = files.filter(f => valueOf(f) !== null && f !== 'en-XA.json')
    expect(carrying.length).toBeGreaterThan(10)
    for (const f of carrying) {
      expect(valueOf(f), f).toContain('(recommended)')
    }
  })

  it('leaves no catalogue with an empty or placeholder explainer', () => {
    for (const f of files) {
      const v = valueOf(f)
      if (v === null) continue
      expect(v.trim().length, f).toBeGreaterThan(20)
      expect(v, f).not.toContain('TODO')
    }
  })
})
