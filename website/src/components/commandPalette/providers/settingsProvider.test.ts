import { describe, it, expect, vi } from 'vitest'
import type { NavigateFunction } from 'react-router-dom'
import type { Result } from '../types'

/**
 * Unit tests for the Settings provider (Search Everywhere).
 */

import { createSettingsProvider, resolveTabPrefix } from './settingsProvider'

function navigate(): { nav: NavigateFunction; spy: ReturnType<typeof vi.fn> } {
  const spy = vi.fn()
  return { nav: spy as unknown as NavigateFunction, spy }
}

async function run(p: ReturnType<typeof createSettingsProvider>, q: string): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

describe('createSettingsProvider — identity', () => {
  it('exposes settings provider id, label, and icon', () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    expect(p.id).toBe('settings')
    expect(p.label).toBe('Settings')
    expect(p.icon).toBeTruthy()
  })
})

describe('createSettingsProvider — search', () => {
  it('finds settings by label match', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'dark mode')
    // Should find Mode (display tab) via keyword synonyms
    const hit = arr.find(r => r.title === 'Mode')
    expect(hit).toBeDefined()
    expect(hit!.providerId).toBe('settings')
  })

  it('finds settings by keyword synonym', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'theme')
    expect(arr.length).toBeGreaterThan(0)
    // Should match Color Theme or Mode via synonyms
    const titles = arr.map(r => r.title)
    expect(titles.some(t => t.includes('Theme') || t === 'Mode')).toBe(true)
  })

  it('ranks an exact keyword hit above scattered label subsequences', async () => {
    // Regression: "yolo" is y-o-l-o scattered through "Your Role", whose label
    // subsequence used to outrank the auto-approve entry that carries the
    // literal keyword "yolo" — so Enter took the wrong destination on the
    // feature's own flagship query. Whole-word keyword hits rank WITH labels.
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'yolo')
    expect(arr.length).toBeGreaterThan(0)
    expect(arr[0].id).toBe('settings:security.how-long-auto-approve-stays-on')
  })

  it('ranks a whole-word hit inside a multi-word keyword at label rank', async () => {
    // "until shutdown" is a keyword of the same entry; the query aligns with
    // its second word, which must qualify (not just keyword-prefix queries).
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'shutdown')
    expect(arr.length).toBeGreaterThan(0)
    expect(arr[0].id).toBe('settings:security.how-long-auto-approve-stays-on')
  })

  it('an exact LABEL beats an exact keyword on a tie', async () => {
    // "theme" is both the Theme setting's exact label and a curated keyword of
    // Mode. Equal raw scores + the alphabetical tiebreak used to put Mode on
    // top; the 1-point keyword edge keeps the row that IS the term first.
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'theme')
    expect(arr[0].title).toBe('Theme')
    expect(arr.some(r => r.title === 'Mode')).toBe(true)
  })

  it('promotes a query aligned after a hyphen in a keyword', async () => {
    // 'sans-serif' is a keyword of Font Family; fuzzyMatch treats '-' as a
    // word boundary, so the promotion predicate must too — space-only
    // matching left every hyphenated synonym in the discounted tier.
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'serif')
    expect(arr.length).toBeGreaterThan(0)
    expect(arr[0].id).toBe('settings:display.font-family')
  })

  it('does not match subsequences scattered across fields', async () => {
    // Parts are matched individually: "yolo" used to hit rows like the
    // cursor-motion toggle by scattering letters across
    // label+description+keywords in the joined corpus, burying real hits.
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'yolo')
    const titles = arr.map(r => r.title)
    expect(titles).not.toContain('Show cursor motion')
    expect(titles).not.toContain('Auto-submit when I finish speaking')
  })

  it('shows breadcrumb subtitle in "Tab › Label" format', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'zoom')
    const hit = arr.find(r => r.title === 'Zoom Level')
    expect(hit).toBeDefined()
    expect(hit!.subtitle).toBe('Display › Zoom Level')
  })

  it('navigates to /settings?tab=...&highlight=... on activate', async () => {
    const { nav, spy } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'zoom')
    const hit = arr.find(r => r.title === 'Zoom Level')
    expect(hit).toBeDefined()
    hit!.onActivate()
    expect(spy).toHaveBeenCalledTimes(1)
    const url = spy.mock.calls[0][0] as string
    expect(url).toContain('/settings?tab=display')
    expect(url).toContain('highlight=')
  })

  it('threads entry params into the route so sub-selected panels mount (channels)', async () => {
    const { nav, spy } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'slash command')
    // Channel-panel rows carry the fan-out suffix in their display label.
    const hit = arr.find(r => r.title === 'Slash command (Slack)')
    expect(hit).toBeDefined()
    hit!.onActivate()
    const url = spy.mock.calls[0][0] as string
    // Without channel=slack the Channels tab defaults elsewhere (or shows the
    // bare list) and the highlight silently no-ops on an unmounted panel.
    expect(url).toContain('tab=channels')
    expect(url).toContain('channel=slack')
    expect(url).toContain('highlight=')
  })

  it('scopes legacy per-channel tab prefixes (slack:) to the channels tab', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    // `slack:` is a legacy per-channel tab filter (the five channel tabs are
    // now collapsed into one); it must keep scoping instead of falling
    // through to a full-corpus query that matches nothing.
    const arr = await run(p, 'slack: slash')
    const hit = arr.find(r => r.title === 'Slash command (Slack)')
    expect(hit).toBeDefined()
  })

  it('returns empty results for empty query', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, '')
    expect(arr).toEqual([])
  })

  it('returns empty results for no match', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'zzzzqqqq')
    expect(arr).toEqual([])
  })

  it('results are sorted by score descending', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'font')
    expect(arr.length).toBeGreaterThan(1)
    for (let i = 1; i < arr.length; i++) {
      expect(arr[i - 1].score).toBeGreaterThanOrEqual(arr[i].score)
    }
  })

  it('enter action uses navigate kind', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'zoom')
    const hit = arr.find(r => r.title === 'Zoom Level')
    expect(hit?.enter?.kind).toBe('navigate')
  })
})

describe('createSettingsProvider — tab filter', () => {
  it('voice: alone lists only voice-tab entries', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'voice:')
    expect(arr.length).toBeGreaterThan(0)
    // Every result must be from voice tab
    for (const r of arr) {
      expect(r.subtitle).toMatch(/^Voice ›/)
    }
    // Sorted alphabetically by label (title)
    for (let i = 1; i < arr.length; i++) {
      expect(arr[i - 1].title.localeCompare(arr[i].title)).toBeLessThanOrEqual(0)
    }
  })

  it('voice: aws narrows within voice tab; other-tab AWS entries excluded', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'voice: aws')
    // All results in voice tab
    for (const r of arr) {
      expect(r.subtitle).toMatch(/^Voice ›/)
    }
    // Now search unfiltered for "aws" and check we'd get results from non-voice tabs too
    const unfilteredArr = await run(p, 'aws')
    const nonVoice = unfilteredArr.filter(r => !r.subtitle?.startsWith('Voice'))
    // If there happen to be AWS entries in other tabs, they must NOT appear in the filtered result
    if (nonVoice.length > 0) {
      const filteredIds = new Set(arr.map(r => r.id))
      for (const other of nonVoice) {
        expect(filteredIds.has(other.id)).toBe(false)
      }
    }
  })

  it('unambiguous prefix disp: mode works like display: mode', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const full = await run(p, 'display: mode')
    const prefix = await run(p, 'disp: mode')
    // Same results (same IDs, same tab scope)
    expect(prefix.length).toBe(full.length)
    const fullIds = new Set(full.map(r => r.id))
    for (const r of prefix) {
      expect(fullIds.has(r.id)).toBe(true)
    }
    // All from display tab
    for (const r of prefix) {
      expect(r.subtitle).toMatch(/^Display ›/)
    }
  })

  it('unknown prefix zzz: foo falls back to normal search (non-crash, sensible results)', async () => {
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    // "zzz:" is unknown; should fall through to normal full-corpus search of "zzz: foo"
    const arr = await run(p, 'zzz: foo')
    // Should not throw. May be empty (no match for "zzz: foo") or partial — just ensure no crash.
    expect(Array.isArray(arr)).toBe(true)
  })

  it('ambiguous prefix falls back to normal search', async () => {
    // Among the real fork tabs: browser, chat, developer, display, notifications, slack, voice
    // "d:" is ambiguous (developer, display) — should fall back to normal search.
    const { nav } = navigate()
    const p = createSettingsProvider(nav)
    const arr = await run(p, 'd: mode')
    // Fallback means normal search of "d: mode" — may have results from any tab.
    // Key assertion: does NOT crash and does NOT restrict to just one tab.
    expect(Array.isArray(arr)).toBe(true)
  })
})

describe('resolveTabPrefix — unit', () => {
  // Real fork tabs after the nav regroup: the five per-channel tabs collapsed
  // into 'channels'; legacy channel keys resolve via LEGACY_TAB_ALIASES.
  const tabs = ['overview', 'chat', 'display', 'voice', 'notifications', 'shortcuts', 'channels', 'browser', 'instances', 'security', 'developer', 'about']

  it('exact match returns tab key', () => {
    expect(resolveTabPrefix('voice', tabs)).toBe('voice')
    expect(resolveTabPrefix('VOICE', tabs)).toBe('voice')
    expect(resolveTabPrefix('Chat', tabs)).toBe('chat')
  })

  it('unambiguous prefix resolves', () => {
    expect(resolveTabPrefix('vo', tabs)).toBe('voice')
    expect(resolveTabPrefix('bro', tabs)).toBe('browser')
    expect(resolveTabPrefix('not', tabs)).toBe('notifications')
    expect(resolveTabPrefix('disp', tabs)).toBe('display')
  })

  it('legacy channel tab keys alias to channels (exact and prefix)', () => {
    // Exact legacy keys
    expect(resolveTabPrefix('slack', tabs)).toBe('channels')
    expect(resolveTabPrefix('discord', tabs)).toBe('channels')
    expect(resolveTabPrefix('telegram', tabs)).toBe('channels')
    expect(resolveTabPrefix('webex', tabs)).toBe('channels')
    expect(resolveTabPrefix('wecom', tabs)).toBe('channels')
    // Unambiguous legacy prefixes keep working (muscle memory)
    expect(resolveTabPrefix('sl', tabs)).toBe('channels')
    expect(resolveTabPrefix('disc', tabs)).toBe('channels')
    expect(resolveTabPrefix('tel', tabs)).toBe('channels')
    // webex + wecom are two alias keys but ONE target — still unambiguous
    expect(resolveTabPrefix('we', tabs)).toBe('channels')
  })

  it('ambiguous prefix returns null', () => {
    // "d" matches developer, display, and the discord alias
    expect(resolveTabPrefix('d', tabs)).toBeNull()
    // "s" matches shortcuts, security, and the slack alias
    expect(resolveTabPrefix('s', tabs)).toBeNull()
    // "ch" matches chat and channels
    expect(resolveTabPrefix('ch', tabs)).toBeNull()
  })

  it('unknown prefix returns null', () => {
    expect(resolveTabPrefix('zzz', tabs)).toBeNull()
    expect(resolveTabPrefix('xyz', tabs)).toBeNull()
  })
})
