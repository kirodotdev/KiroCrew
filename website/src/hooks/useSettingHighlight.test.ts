import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { createElement } from 'react'
import { useSettingHighlight } from './useSettingHighlight'

// Mock scrollIntoView (not available in jsdom)
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

// Three tests here install fake timers and restore them INLINE on their last
// line. An assertion that fails before that line leaves fake timers installed for
// every LATER test in the file, so one real failure reports as a cascade of
// unrelated timeouts and the actual cause is buried. Restoring here makes that
// impossible; it is idempotent when timers were never faked.
afterEach(() => {
  vi.useRealTimers()
})

function wrapper(initialEntries: string[]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(MemoryRouter, { initialEntries }, children)
  }
}

describe('useSettingHighlight', () => {
  it('does nothing when no highlight param is present', () => {
    const { result } = renderHook(() => useSettingHighlight(), {
      wrapper: wrapper(['/settings?tab=display']),
    })
    expect(result.current).toBeUndefined()
  })

  it('strips unknown highlight ids from the URL', async () => {
    vi.useFakeTimers()
    let currentSearch = 'unset'

    function CaptureWrapper({ children }: { children: ReactNode }) {
      return createElement(
        MemoryRouter,
        { initialEntries: ['/settings?tab=display&highlight=nonexistent.setting'] },
        createElement(LocationProbe, null, children),
      )
    }
    function LocationProbe({ children }: { children?: ReactNode }) {
      currentSearch = useLocation().search
      return createElement('div', null, children)
    }

    renderHook(() => useSettingHighlight(), { wrapper: CaptureWrapper })
    act(() => {
      vi.advanceTimersByTime(150)
    })
    expect(currentSearch).not.toContain('highlight=')
    expect(currentSearch).toContain('tab=display')
    vi.useRealTimers()
  })

  it('highlights the Nth occurrence for duplicate labels', async () => {
    vi.useFakeTimers()

    // Create two elements with the same data-setting-label (simulates duplicate within a tab)
    const el1 = document.createElement('div')
    el1.setAttribute('data-setting-label', 'AWS Profile')
    document.body.appendChild(el1)
    const el2 = document.createElement('div')
    el2.setAttribute('data-setting-label', 'AWS Profile')
    document.body.appendChild(el2)

    // Find the registry entry with occurrence 2 for 'AWS Profile' (voice.aws-profile-2)
    const { SETTINGS_REGISTRY } = await import('../components/commandPalette/settingsRegistry.gen')
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'voice.aws-profile-2')
    // If this entry doesn't exist in the test environment, skip gracefully
    if (entry) {
      renderHook(() => useSettingHighlight(), {
        wrapper: wrapper([`/settings?tab=voice&highlight=voice.aws-profile-2`]),
      })
      act(() => {
        vi.advanceTimersByTime(150)
      })
      // The second element should get the highlight (occurrence=2 → index 1),
      // the first should not. Assert on the two highlight properties happy-dom
      // serializes faithfully — `outlineOffset` ('4px') AND `borderRadius`
      // ('8px') — rather than the `outline` shorthand: happy-dom mis-parses
      // `outline: 2px solid var(--accent)` (the var() throws off its shorthand
      // splitter), so `.style.outline` is unreliable there. Checking two distinct
      // highlight props (not just one) keeps the test tied to the highlight
      // actually applying, not an incidental single property.
      expect(el2.style.outlineOffset).toBe('4px')
      expect(el2.style.borderRadius).toBe('8px')
      expect(el1.style.outlineOffset).toBe('')
      expect(el1.style.borderRadius).toBe('')
    }

    document.body.removeChild(el1)
    document.body.removeChild(el2)
    vi.useRealTimers()
  })
})

describe('useSettingHighlight against the real registry and catalogs', () => {
  /*
   * Deliberately UNMOCKED, which is the whole point of this case.
   *
   * `useSettingHighlightKeyPrefix.test.ts` pins the same path with a stubbed
   * registry entry and a stubbed `i18nT`, so it proves the hook consults the key
   * and uses what it gets back. It cannot fail if the generator recorded the
   * WRONG key, or a key the shipped catalogs do not carry — both are supplied by
   * the mock. This one runs the real generated registry through the real
   * catalogs under a real language switch, so those two failures are reachable.
   */
  afterAll(async () => {
    const { i18next } = await import('../i18n/all')
    await i18next.changeLanguage('en')
  })

  function settingControl(label: string, { configKey, settingId }: { configKey?: string; settingId?: string }) {
    const el = document.createElement('div')
    if (configKey) el.dataset.settingKey = configKey
    if (settingId) el.dataset.settingId = settingId
    el.dataset.settingLabel = label
    el.scrollIntoView = vi.fn()
    return el
  }

  it.each([
    { name: 'TTS with keyed STT', highlight: 'voice.provider-2', target: { settingId: 'voice.provider-2' }, sibling: { configKey: 'stt.provider' } },
    { name: 'TTS with unkeyed legacy row', highlight: 'voice.provider-2', target: { settingId: 'voice.provider-2' }, sibling: {} },
    { name: 'STT with explicit TTS', highlight: 'key:stt.provider', target: { configKey: 'stt.provider' }, sibling: { settingId: 'voice.provider-2' } },
  ])('waits for the exact cold provider during $name', async ({ highlight, target, sibling }) => {
    vi.useFakeTimers()
    const otherProvider = settingControl('Provider', sibling)
    const targetProvider = settingControl('Provider', target)
    document.body.appendChild(otherProvider)
    const view = renderHook(() => {
      useSettingHighlight()
      return useLocation().search
    }, { wrapper: wrapper([`/settings/voice?highlight=${highlight}`]) })

    try {
      act(() => { vi.advanceTimersByTime(150) })
      expect(otherProvider.scrollIntoView).not.toHaveBeenCalled()
      expect(view.result.current).toContain('highlight=')

      // The configuration query settles after the initial highlight tick.
      await act(async () => { document.body.appendChild(targetProvider) })
      expect(targetProvider.scrollIntoView).toHaveBeenCalledTimes(1)
      expect(view.result.current).not.toContain('highlight=')
      // Consuming the URL must leave the highlight visible for its fade period.
      expect(targetProvider.style.outlineOffset).toBe('4px')

      await act(async () => { document.body.removeChild(otherProvider) })
      expect(targetProvider.scrollIntoView).toHaveBeenCalledTimes(1)
    } finally {
      view.unmount()
      otherProvider.remove()
      targetProvider.remove()
      vi.clearAllTimers()
    }
  })

  it.each(['navigation', 'unmount'])('abandons a pending UI target on %s', async (end) => {
    vi.useFakeTimers()
    const playback = settingControl('Provider', { settingId: 'voice.provider-2' })
    const preventSleep = settingControl('Prevent sleep while running', { configKey: 'dashboard.prevent_sleep' })
    const view = renderHook(() => {
      useSettingHighlight()
      return { search: useLocation().search, navigate: useNavigate() }
    }, { wrapper: wrapper(['/settings/voice?highlight=voice.provider-2']) })

    try {
      act(() => { vi.advanceTimersByTime(150) })
      expect(view.result.current.search).toContain('highlight=')
      if (end === 'unmount') view.unmount()
      else {
        act(() => {
          view.result.current.navigate('/settings/chat?highlight=key:dashboard.prevent_sleep&keep=1')
        })
        act(() => { vi.advanceTimersByTime(150) })
        expect(new URLSearchParams(view.result.current.search).get('highlight')).toBe('key:dashboard.prevent_sleep')
      }

      await act(async () => {
        document.body.append(playback, preventSleep)
      })
      expect(playback.scrollIntoView).not.toHaveBeenCalled()
      if (end === 'navigation') {
        expect(preventSleep.scrollIntoView).toHaveBeenCalledTimes(1)
        expect(view.result.current.search).toBe('?keep=1')
      } else expect(preventSleep.scrollIntoView).not.toHaveBeenCalled()
    } finally {
      view.unmount()
      playback.remove()
      preventSleep.remove()
      vi.clearAllTimers()
    }
  })

  it('highlights a control whose rendered label is translated', async () => {
    // `/all` for the Japanese catalog: `../i18n` registers English only, so the
    // switch below would fall back to English and the label match would be
    // self-consistently wrong.
    const { i18next } = await import('../i18n/all')
    const { i18nT } = await import('../i18n/t')
    const { SETTINGS_REGISTRY } = await import('../components/commandPalette/settingsRegistry.gen')

    // `display.language` carries no configKey, so the label-derived id is its
    // only route — and it is one of the anchors the curated tips pool ships.
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'display.language')
    expect(entry, 'display.language must exist in the registry').toBeTruthy()
    expect(entry!.labelKey, 'the generator must record its catalog key').toBeTruthy()

    await i18next.changeLanguage('ja')
    const translated = i18nT(entry!.labelKey as Parameters<typeof i18nT>[0])
    // Two guards, because the DOM label below is derived from this same call:
    // without them a self-consistent wrong answer would satisfy the match.
    //
    //  - not the English label: proves the active locale is really in play.
    //  - not the key itself: `i18nT` returns the raw key when NEITHER the active
    //    catalog nor English carries it, so this is what fails if the generator
    //    ever records a key the shipped catalogs do not have.
    expect(translated).not.toBe(entry!.label)
    expect(translated).not.toBe(entry!.labelKey)

    const el = document.createElement('div')
    el.setAttribute('data-setting-label', translated)
    document.body.appendChild(el)

    vi.useFakeTimers()
    renderHook(() => useSettingHighlight(), {
      wrapper: wrapper(['/settings?tab=display&highlight=display.language']),
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    // The same two properties the duplicate-label case asserts on, for the same
    // happy-dom shorthand reason.
    expect(el.style.outlineOffset).toBe('4px')
    expect(el.style.borderRadius).toBe('8px')

    vi.useRealTimers()
    document.body.removeChild(el)
  })
})
