/**
 * useSettingHighlight -- the outline's fade timers.
 *
 * A highlight shows a 2 s outline, then fades it over 300 ms. A second jump to
 * the same setting inside that window must get its own full 2 s, not be cut
 * short by the first jump's fade. And when the hook unmounts, its pending fade
 * is cleared and the outline is removed, so no timer fires after the page is gone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { createElement } from 'react'
import { useSettingHighlight } from '../hooks/useSettingHighlight'

vi.mock('../components/commandPalette/settingsRegistry.gen', () => ({
  SETTINGS_REGISTRY: [
    {
      id: 'chat.default-model',
      label: 'Default Model',
      tab: 'chat',
      type: 'select',
      occurrence: 1,
      configKey: 'chat.default_model',
    },
  ],
}))

const LINK = '/settings?tab=chat&highlight=key:chat.default_model'

function wrapper({ children }: { children: ReactNode }) {
  return createElement(MemoryRouter, { initialEntries: [LINK] }, children)
}

let el: HTMLDivElement

beforeEach(() => {
  vi.useFakeTimers()
  Element.prototype.scrollIntoView = vi.fn()
  el = document.createElement('div')
  el.setAttribute('data-setting-key', 'chat.default_model')
  document.body.appendChild(el)
})

afterEach(() => {
  el.remove()
  vi.useRealTimers()
})

describe('useSettingHighlight fade timers', () => {
  it('gives a second jump to the same setting its own full outline', () => {
    const { result } = renderHook(
      () => {
        useSettingHighlight()
        return useNavigate()
      },
      { wrapper },
    )
    act(() => { vi.advanceTimersByTime(150) })
    expect(el.style.outlineOffset).toBe('4px')

    // Jump again 1.5 s in, while the first outline is still showing.
    act(() => { vi.advanceTimersByTime(1500) })
    act(() => { result.current(LINK) })
    act(() => { vi.advanceTimersByTime(150) })

    // 900 ms after the second jump: the first jump's fade (due 2 s after it)
    // has come and gone, but the second jump's outline must still be up.
    act(() => { vi.advanceTimersByTime(750) })
    expect(el.style.outlineOffset).toBe('4px')
    expect(el.style.outlineColor).not.toBe('transparent')

    // The second jump's own fade still runs.
    act(() => { vi.advanceTimersByTime(2000) })
    expect(el.style.outline).toBe('')
  })

  it('clears the pending fade and the outline on unmount', () => {
    const { unmount } = renderHook(() => useSettingHighlight(), { wrapper })
    act(() => { vi.advanceTimersByTime(150) })
    expect(el.style.outlineOffset).toBe('4px')

    unmount()
    expect(el.style.outline).toBe('')
    expect(el.style.outlineOffset).toBe('')
    expect(vi.getTimerCount()).toBe(0)
  })
})
