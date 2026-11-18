/**
 * Every ChatInput host (the Sessions chat, a Bot's chat, a split-view pane, a
 * pop-out crew window) resolves its Settings -> Chat display values in one
 * place, `useComposerSettings`: the host's prop when it passes one, the user's
 * stored choice otherwise. A Bot's chat passes none of them, so this fallback is
 * how Show Pasted Text in Full and the context readout reach it.
 */
import { act, renderHook } from '@testing-library/react'
import { useComposerSettings } from '../components/chat-input/composerSettings'

const seed = (cfg: Record<string, unknown>) => localStorage.setItem('mc-chat-config', JSON.stringify(cfg))

describe('useComposerSettings', () => {
  it('falls back to the stored choices when the host passes no prop', () => {
    seed({ showFullPastes: true, showContextPct: true, showContextTokens: true })
    const { result } = renderHook(() => useComposerSettings({}))
    expect(result.current).toEqual({ showFullPastes: true, showContextPct: true, showContextTokens: true })
  })

  it('uses the defaults with nothing stored', () => {
    const { result } = renderHook(() => useComposerSettings({}))
    expect(result.current).toEqual({ showFullPastes: false, showContextPct: false, showContextTokens: false })
  })

  it("keeps the host's explicit values", () => {
    seed({ showFullPastes: true, showContextPct: true, showContextTokens: true })
    const { result } = renderHook(() => useComposerSettings({ showFullPastes: false, showContextPct: false, showContextTokens: false }))
    expect(result.current).toEqual({ showFullPastes: false, showContextPct: false, showContextTokens: false })
  })

  it('follows a change saved while the composer is mounted', () => {
    const { result } = renderHook(() => useComposerSettings({}))
    expect(result.current.showFullPastes).toBe(false)
    act(() => {
      seed({ showFullPastes: true })
      window.dispatchEvent(new Event('mc-config-changed'))
    })
    expect(result.current.showFullPastes).toBe(true)
  })

  it('follows a change saved in another tab', () => {
    const { result } = renderHook(() => useComposerSettings({}))
    act(() => {
      seed({ showContextPct: true })
      window.dispatchEvent(new StorageEvent('storage', { key: 'mc-chat-config' }))
    })
    expect(result.current.showContextPct).toBe(true)
  })
})
