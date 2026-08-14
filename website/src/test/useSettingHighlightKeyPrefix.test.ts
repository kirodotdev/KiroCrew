/**
 * useSettingHighlight — ?highlight=key:<configKey> format.
 *
 * Verifies that the key: prefix resolves a dotted config key to the
 * corresponding registry entry's id, then proceeds with the standard
 * label-based DOM highlight. Legacy label-id format must remain working.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { createElement } from 'react'
import { useSettingHighlight, resolveLegacyHighlightId } from '../hooks/useSettingHighlight'

// We need to mock the registry to have a configKey entry for testing
vi.mock('../components/commandPalette/settingsRegistry.gen', () => ({
  SETTINGS_REGISTRY: [
    {
      id: 'chat.fallback-model',
      label: 'Fallback Model',
      tab: 'chat',
      type: 'select',
      occurrence: 1,
      configKey: 'chat.default_model',
    },
    {
      id: 'channels.phase-reactions',
      label: 'Phase reactions',
      tab: 'channels',
      type: 'toggle',
      occurrence: 1,
      configKey: 'slack.phase_reactions',
    },
  ],
}))

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

function wrapper(initialEntries: string[]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(MemoryRouter, { initialEntries }, children)
  }
}

describe('useSettingHighlight key: prefix', () => {
  it('resolves key:configKey via data-setting-key attribute directly (no label round-trip)', async () => {
    vi.useFakeTimers()

    // Create DOM element with matching data-setting-key (direct path)
    const el = document.createElement('div')
    el.setAttribute('data-setting-key', 'chat.default_model')
    el.setAttribute('data-setting-label', 'Fallback Model')
    document.body.appendChild(el)

    renderHook(() => useSettingHighlight(), {
      wrapper: wrapper(['/settings?tab=chat&highlight=key:chat.default_model']),
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    // Should have applied the highlight style via data-setting-key lookup
    expect(el.style.outlineOffset).toBe('4px')
    expect(el.style.borderRadius).toBe('8px')

    document.body.removeChild(el)
    vi.useRealTimers()
  })

  it('falls back to label resolution when data-setting-key attribute is absent', async () => {
    vi.useFakeTimers()

    // Element only has data-setting-label, no data-setting-key
    const el = document.createElement('div')
    el.setAttribute('data-setting-label', 'Fallback Model')
    document.body.appendChild(el)

    renderHook(() => useSettingHighlight(), {
      wrapper: wrapper(['/settings?tab=chat&highlight=key:chat.default_model']),
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    // Should still highlight via legacy label fallback
    expect(el.style.outlineOffset).toBe('4px')
    expect(el.style.borderRadius).toBe('8px')

    document.body.removeChild(el)
    vi.useRealTimers()
  })

  it('strips param for unknown configKey with key: prefix', async () => {
    vi.useFakeTimers()
    let currentSearch = 'unset'

    function CaptureWrapper({ children }: { children: ReactNode }) {
      return createElement(
        MemoryRouter,
        { initialEntries: ['/settings?tab=chat&highlight=key:nonexistent.key.here'] },
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
    // highlight param should be stripped (unknown key)
    expect(currentSearch).not.toContain('highlight=')
    expect(currentSearch).toContain('tab=chat')
    vi.useRealTimers()
  })

  it('legacy id format still works (resolveLegacyHighlightId)', () => {
    expect(resolveLegacyHighlightId('chat.default-model')).toBe('chat.fallback-model')
    expect(resolveLegacyHighlightId('slack.phase-reactions')).toBe('channels.phase-reactions')
  })

  it('highlights via key: prefix for channels tab setting', async () => {
    vi.useFakeTimers()

    const el = document.createElement('div')
    el.setAttribute('data-setting-label', 'Phase reactions')
    document.body.appendChild(el)

    renderHook(() => useSettingHighlight(), {
      wrapper: wrapper(['/settings?tab=channels&highlight=key:slack.phase_reactions']),
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    expect(el.style.outlineOffset).toBe('4px')
    expect(el.style.borderRadius).toBe('8px')

    document.body.removeChild(el)
    vi.useRealTimers()
  })
})
