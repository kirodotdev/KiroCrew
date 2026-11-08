import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  loadSoundSettings,
  themeTriggerForNotification,
  useNotificationSound,
  __resetForTests,
  type SoundSettings,
} from '../hooks/useNotificationSound'
import { MC_NOTIFICATION_EVENT } from '../hooks/notificationEvent'
import { activeThemeSoundTriggers, publishThemeSoundTriggers } from '../hooks/themeSound'

const STORAGE_KEY = 'mc-notification-sound'

const mockCtx = {
  state: 'running' as const,
  currentTime: 0,
  destination: {},
  resume: vi.fn(() => Promise.resolve()),
  createOscillator: vi.fn(() => ({
    connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(),
    type: '', frequency: { value: 0 }, onended: null,
  })),
  createGain: vi.fn(() => ({
    gain: { setValueCurveAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
    connect: vi.fn(),
    disconnect: vi.fn(),
  })),
}
;(window as unknown as { AudioContext: unknown }).AudioContext = vi.fn(function () { return mockCtx })

const settings = (perCategory: SoundSettings['perCategory'] = { all: 'chime' }): SoundSettings => ({
  enabled: true,
  volume: 0.5,
  perCategory,
})

let release: (() => void) | null = null

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  mockCtx.createOscillator.mockClear()
  release?.()
  release = null
})

function notifyAndCountTones(kind: string): number {
  const { unmount } = renderHook(() => useNotificationSound())
  const before = mockCtx.createOscillator.mock.calls.length
  window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind } }))
  const played = mockCtx.createOscillator.mock.calls.length - before
  unmount()
  return played
}

describe('themeTriggerForNotification', () => {
  it('returns null when the theme publishes no triggers', () => {
    expect(themeTriggerForNotification('cron', settings(), new Set())).toBeNull()
  })

  it('prefers the category trigger, then the generic notification trigger', () => {
    const both = new Set(['category:approval', 'notification'])
    expect(themeTriggerForNotification('approval', settings(), both)).toBe('category:approval')
    expect(themeTriggerForNotification('cron', settings(), both)).toBe('notification')
    expect(themeTriggerForNotification('not-a-category', settings(), both)).toBe('notification')
    expect(themeTriggerForNotification(undefined, settings(), both)).toBe('notification')
  })

  it('returns null when the theme has neither the category nor the notification trigger', () => {
    const onlyApproval = new Set(['category:approval', 'message-received'])
    expect(themeTriggerForNotification('cron', settings(), onlyApproval)).toBeNull()
  })

  it('leaves a category the user set explicitly to its own sound', () => {
    const both = new Set(['category:approval', 'notification'])
    expect(themeTriggerForNotification('approval', settings({ all: 'chime', approval: 'ding' }), both)).toBeNull()
    expect(themeTriggerForNotification('approval', settings({ all: 'chime', approval: 'none' }), both)).toBeNull()
  })

  it('does not treat the all fallback as a choice for one category', () => {
    const generic = new Set(['notification'])
    expect(themeTriggerForNotification('cron', settings({ all: 'ding' }), generic)).toBe('notification')
  })
})

describe('publishThemeSoundTriggers', () => {
  it('a stale release does not withdraw a newer publication', () => {
    const first = publishThemeSoundTriggers(['notification'])
    release = publishThemeSoundTriggers(['category:cron'])
    first()
    expect([...activeThemeSoundTriggers()]).toEqual(['category:cron'])
    release()
    release = null
    expect(activeThemeSoundTriggers().size).toBe(0)
  })
})

describe('useNotificationSound with an active theme', () => {
  it('skips the built-in tone when the theme provides a notification sound', () => {
    release = publishThemeSoundTriggers(['notification'])
    expect(notifyAndCountTones('cron')).toBe(0)
  })

  it('skips the built-in tone when the theme provides the category sound', () => {
    release = publishThemeSoundTriggers(['category:approval'])
    expect(notifyAndCountTones('approval')).toBe(0)
  })

  it('plays the built-in tone when the theme has no notification sound', () => {
    release = publishThemeSoundTriggers(['message-received', 'activate'])
    expect(notifyAndCountTones('cron')).toBeGreaterThan(0)
  })

  it('plays the built-in tone when no theme sound is published', () => {
    expect(notifyAndCountTones('cron')).toBeGreaterThan(0)
  })

  it('plays the built-in tone for a category the user set explicitly', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ perCategory: { all: 'chime', approval: 'ding' } }))
    expect(loadSoundSettings().perCategory.approval).toBe('ding')
    release = publishThemeSoundTriggers(['category:approval', 'notification'])
    expect(notifyAndCountTones('approval')).toBeGreaterThan(0)
  })
})
