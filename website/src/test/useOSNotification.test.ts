/**
 * useOSNotification — the OS banner surface over MC_NOTIFICATION_EVENT.
 *
 * Contract under test:
 * - fires exactly one banner per event (single consumer = no duplicates);
 * - forwards the note's title/body/tag as-is; falls back to localized labels
 *   for the synthesized `turn` / `approval` kinds;
 * - prefixes the owning session's title so multi-session users can tell
 *   which session is talking;
 * - gates on: master switch, per-category switch, document.hidden, and
 *   Notification.permission === 'granted';
 * - settings round-trip through localStorage and hot-reload on the
 *   settings-changed event.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import {
  useOSNotification, loadOSSettings, saveOSSettings,
  osBannerWantedForKind, composeBanner,
} from '../hooks/useOSNotification'
import { sseSlots } from '../store/dashboardSlice'
import { MC_NOTIFICATION_EVENT, type McNotificationDetail } from '../hooks/notificationEvent'
import type { ChatSlot } from '../types'

const constructed: { title: string; options: NotificationOptions }[] = []

class MockNotification {
  static permission: NotificationPermission = 'granted'
  static requestPermission = vi.fn()
  onclick: (() => void) | null = null
  close = vi.fn()
  constructor(title: string, options: NotificationOptions) {
    constructed.push({ title, options })
  }
}

function fire(detail: McNotificationDetail) {
  act(() => {
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
  })
}

describe('useOSNotification', () => {
  let store: ReturnType<typeof createTestStore>

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(MemoryRouter, null, children))
    }
    return renderHook(() => useOSNotification('Kiro Crew', '/avatar.png'), { wrapper })
  }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    constructed.length = 0
    MockNotification.permission = 'granted'
    vi.stubGlobal('Notification', MockNotification)
    // Banner path requires a hidden tab.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    store = createTestStore()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  it('mirrors a bus note as one banner with its own title/body/tag', () => {
    mount()
    fire({ kind: 'cron', title: 'Nightly backup', body: 'Completed in 42s', tag: 'job-7' })
    expect(constructed).toHaveLength(1)
    expect(constructed[0].title).toBe('Nightly backup')
    expect(constructed[0].options.body).toBe('Completed in 42s')
    expect(constructed[0].options.tag).toBe('job-7')
  })

  it('prefixes the owning session title', () => {
    store.dispatch(sseSlots([{ key: 's1', title: 'Refactor billing', messages: 1, running: false } as ChatSlot]))
    mount()
    fire({ kind: 'approval', body: 'Bash', slot: 's1', tag: 'kirocrew-approval-1' })
    expect(constructed).toHaveLength(1)
    expect(constructed[0].title).toContain('Refactor billing')
    expect(constructed[0].options.body).toBe('Bash')
  })

  it('synthesizes a banner for turn completion from the session title', () => {
    store.dispatch(sseSlots([{ key: 's2', title: 'Fix CI', messages: 1, running: false } as ChatSlot]))
    mount()
    fire({ kind: 'turn', slot: 's2', tag: 'kirocrew-turn-s2' })
    expect(constructed).toHaveLength(1)
    expect(constructed[0].title).toBe('Fix CI')
    expect(constructed[0].options.body).toBeTruthy()
  })

  it('does nothing when the master switch is off', () => {
    saveOSSettings({ enabled: false, perCategory: {} })
    mount()
    fire({ kind: 'cron', title: 'T', body: 'B' })
    expect(constructed).toHaveLength(0)
  })

  it('does nothing for a category switched off, still banners others', () => {
    saveOSSettings({ enabled: true, perCategory: { cron: false } })
    mount()
    fire({ kind: 'cron', title: 'T', body: 'B' })
    fire({ kind: 'approval', body: 'Bash', tag: 'a-1' })
    expect(constructed).toHaveLength(1)
    expect(constructed[0].options.tag).toBe('a-1')
  })

  it('hot-reloads settings on the settings-changed event', () => {
    mount()
    fire({ kind: 'cron', title: 'T1', body: 'B' })
    act(() => { saveOSSettings({ enabled: false, perCategory: {} }) })
    fire({ kind: 'cron', title: 'T2', body: 'B' })
    expect(constructed).toHaveLength(1)
    expect(constructed[0].title).toBe('T1')
  })

  it('does nothing while the tab is visible', () => {
    delete (document as { hidden?: boolean }).hidden
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    mount()
    fire({ kind: 'cron', title: 'T', body: 'B' })
    expect(constructed).toHaveLength(0)
  })

  it('does nothing without granted permission, and never prompts from the listener', () => {
    MockNotification.permission = 'default'
    mount()
    fire({ kind: 'cron', title: 'T', body: 'B' })
    expect(constructed).toHaveLength(0)
    // Permission prompts belong to the Settings toggle, not to event time.
    expect(MockNotification.requestPermission).not.toHaveBeenCalled()
  })

  it('unknown kinds are gated by the master switch only', () => {
    saveOSSettings({ enabled: true, perCategory: { cron: false } })
    mount()
    fire({ kind: 'oncall-radar.ticket-update', title: 'T', body: 'B' })
    expect(constructed).toHaveLength(1)
  })
})

describe('OS settings round-trip', () => {
  beforeEach(() => { localStorage.clear() })

  it('defaults: enabled, no per-category opt-outs', () => {
    const s = loadOSSettings()
    expect(s.enabled).toBe(true)
    expect(s.perCategory).toEqual({})
  })

  it('persists and restores, dropping invalid entries', () => {
    saveOSSettings({ enabled: false, perCategory: { turn: false } })
    localStorage.setItem('mc-notification-os', JSON.stringify({
      enabled: false,
      perCategory: { turn: false, bogus: false, cron: 'nope' },
    }))
    const s = loadOSSettings()
    expect(s.enabled).toBe(false)
    expect(s.perCategory).toEqual({ turn: false })
  })

  it('osBannerWantedForKind: master off beats everything', () => {
    expect(osBannerWantedForKind('approval', { enabled: false, perCategory: {} })).toBe(false)
    expect(osBannerWantedForKind('approval', { enabled: true, perCategory: { approval: false } })).toBe(false)
    expect(osBannerWantedForKind('approval', { enabled: true, perCategory: {} })).toBe(true)
    expect(osBannerWantedForKind(undefined, { enabled: true, perCategory: {} })).toBe(true)
  })
})

describe('composeBanner', () => {
  it('keeps the note title and appends the session name', () => {
    const r = composeBanner({ kind: 'cron', title: 'Backup done', body: 'ok' }, 'Kiro Crew', 'My session')
    expect(r.title).toBe('My session — Backup done')
    expect(r.body).toBe('ok')
  })

  it('falls back to botName for untitled notes without a session', () => {
    const r = composeBanner({ kind: 'whatever' }, 'Kiro Crew')
    expect(r.title).toBe('Kiro Crew')
    expect(r.body).toBeTruthy()
  })
})
