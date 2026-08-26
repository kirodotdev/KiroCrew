import { describe, it, expect } from 'vitest'

import {
  AGENT, HKEY, JOBKEY, SLOTSKEY, LIVEKEY, LIVE_TTL_MS, RAIL_W, MAX_SCREENS, HARD_CAP_MS,
  SEV, sevOf, KIND_LABEL, KIND_LABEL_KEY, kindLabel,
  STAGES, WRITING_STAGE, SCAN_STAGES, BLOCKED, blockedFor, KIND_WAIT,
  SAMPLE_SCREENS, SAMPLE_REPORT,
} from '../apps/design-critique/constants'
import type { SeverityKey } from '../apps/design-critique/types'

/**
 * Every copy field in this module sits behind a GETTER so `i18nT()` runs on
 * property ACCESS rather than once at import (the frozen-at-boot-language bug).
 * A getter that is never read is a getter that could be a frozen literal and
 * nobody would notice, so this reads all of them — and pins the two lookups
 * that must NOT resolve through Object.prototype.
 */
function resolved(value: string): boolean {
  // A missing catalog entry makes i18nT hand the KEY back; a resolved one never
  // looks like a dotted key path.
  return typeof value === 'string' && value.length > 0 && !value.startsWith('apps.designCritique.')
}

describe('design-critique protocol constants', () => {
  it('runs as the core agent, not a bundled persona (a builtin never registers one)', () => {
    expect(AGENT).toBe('kirocrew')
  })

  it('keeps every storage key distinct', () => {
    const keys = [HKEY, JOBKEY, SLOTSKEY, LIVEKEY]
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('caps a live run well inside the stuck-job backstop', () => {
    expect(LIVE_TTL_MS).toBeGreaterThan(HARD_CAP_MS)
    expect(MAX_SCREENS).toBeGreaterThan(1)
    expect(RAIL_W).toMatch(/px$/)
  })
})

describe('severity table', () => {
  it('resolves a label per severity, ranked catastrophe → cosmetic', () => {
    const keys: SeverityKey[] = ['catastrophe', 'major', 'minor', 'cosmetic']
    const ranks = keys.map(k => {
      expect(resolved(SEV[k].label)).toBe(true)
      expect(SEV[k].icon).toBeTruthy()
      return SEV[k].rank
    })
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b))
  })

  it('falls back to cosmetic for an unknown or missing severity', () => {
    expect(sevOf('major')).toBe(SEV.major)
    expect(sevOf('zzz-unknown')).toBe(SEV.cosmetic)
    expect(sevOf(undefined)).toBe(SEV.cosmetic)
  })
})

describe('kind labels', () => {
  it('keeps KIND_LABEL in English (it is spliced into an English prompt)', () => {
    expect(KIND_LABEL.figma).toBe('Figma file')
    expect(KIND_LABEL.url).toBe('running app')
  })

  it('localises the SHOWN kind through the catalog keys', () => {
    for (const kind of Object.keys(KIND_LABEL_KEY)) {
      expect(resolved(kindLabel(kind))).toBe(true)
    }
  })

  it('returns "" for an unknown, missing, or prototype-member kind', () => {
    expect(kindLabel(undefined)).toBe('')
    expect(kindLabel('')).toBe('')
    expect(kindLabel('zzz-unknown')).toBe('')
    // hasOwnProperty, not `in`: a persisted 'toString' must not hand i18next a function.
    expect(kindLabel('toString')).toBe('')
  })
})

describe('waiting stages', () => {
  it('resolves every stage label and keeps the thresholds ascending', () => {
    const thresholds = STAGES.map(s => {
      expect(resolved(s.label)).toBe(true)
      return s.at
    })
    expect(thresholds).toEqual([...thresholds].sort((a, b) => a - b))
    expect(resolved(WRITING_STAGE.label)).toBe(true)
  })

  it('resolves every scan-stage label', () => {
    for (const stage of SCAN_STAGES) expect(resolved(stage.label)).toBe(true)
    expect(SCAN_STAGES[0].at).toBe(0)
  })

  it('resolves the per-kind waiting copy', () => {
    for (const kind of ['figma', 'repo', 'local', 'url'] as const) {
      expect(resolved(KIND_WAIT[kind])).toBe(true)
    }
  })
})

describe('blocked reasons', () => {
  it('resolves say/hint for every reason and names a fix route', () => {
    for (const [reason, entry] of Object.entries(BLOCKED)) {
      expect(resolved(entry.say), reason).toBe(true)
      expect(resolved(entry.hint), reason).toBe(true)
      expect(['local', 'retype', 'shots', 'retry']).toContain(entry.fix)
    }
  })

  it('resolves the auth block, leaving shell commands verbatim', () => {
    const auth = BLOCKED['no-access'].auth!
    expect(resolved(auth.lead)).toBe(true)
    expect(resolved(auth.tail)).toBe(true)
    // A translated command does not run.
    expect(auth.cmds).toContain('gh auth login')
  })

  it('resolves the numbered Figma permission steps as copy', () => {
    const auth = BLOCKED['figma-no-permission'].auth!
    expect(auth.cmds).toHaveLength(3)
    for (const step of auth.cmds) expect(resolved(step)).toBe(true)
  })

  it('blockedFor falls back to `other` for unknown, missing, or prototype keys', () => {
    expect(blockedFor(undefined).say).toBe(BLOCKED.other.say)
    expect(blockedFor('zzz-unknown').say).toBe(BLOCKED.other.say)
    expect(blockedFor('toString').say).toBe(BLOCKED.other.say)
  })

  it('blockedFor returns a detached copy, so a caller cannot mutate the table', () => {
    const entry = blockedFor('no-access')
    expect(entry.say).toBe(BLOCKED['no-access'].say)
    expect(entry.auth).not.toBe(BLOCKED['no-access'].auth)

    entry.detail = 'zzz-detail'
    entry.auth!.lead = 'zzz-overwritten'
    expect(BLOCKED['no-access'].auth!.lead).not.toBe('zzz-overwritten')
    expect(BLOCKED['no-access'].detail).toBeUndefined()
  })

  it('omits the auth block for reasons that have none', () => {
    expect(blockedFor('not-found').auth).toBeUndefined()
  })
})

describe('bundled sample', () => {
  it('is a four-step flow whose images come from the app assets', () => {
    expect(SAMPLE_SCREENS.map(s => s.step)).toEqual([1, 2, 3, 4])
    for (const screen of SAMPLE_SCREENS) {
      expect(screen.url).toMatch(/^\/app-assets\/design-critique\/samples\/.+\.png$/)
    }
  })

  it('has a tally that agrees with its findings, and one screen row per screen', () => {
    const counted = SAMPLE_REPORT.findings.reduce<Record<string, number>>((acc, f) => {
      acc[f.severity] = (acc[f.severity] ?? 0) + 1
      return acc
    }, {})
    expect(counted.catastrophe ?? 0).toBe(SAMPLE_REPORT.tally.catastrophe)
    expect(counted.major).toBe(SAMPLE_REPORT.tally.major)
    expect(counted.minor).toBe(SAMPLE_REPORT.tally.minor)
    expect(counted.cosmetic).toBe(SAMPLE_REPORT.tally.cosmetic)
    expect(SAMPLE_REPORT.screens).toHaveLength(SAMPLE_SCREENS.length)
  })
})
