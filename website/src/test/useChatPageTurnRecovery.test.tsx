/**
 * Turn recovery (pages/chat/page/turnRecovery.ts): the fixes an error row
 * offers, driven as a hook with spies for the model picker and navigation.
 *
 * ChatPage-level suites pin Continue itself (continueGate, refusedPress); this
 * file pins the four error-row fixes, which no page test presses:
 *  - "Pick a model" anchors the picker to the composer's model chip when it is
 *    on screen, to the composer edge otherwise, and never returns focus to the
 *    composer (it was opened from a transcript row);
 *  - the Default Model, Kiro sign-in and crewmate-capabilities links route to
 *    the exact settings/developer/crew paths their rows promise.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'

import { useTurnRecovery } from '../pages/chat/page/turnRecovery'
import { settingsPath } from '../components/settingsPath'
import { SETTINGS_DEFAULT_MODEL_ID } from '../hooks/useSettingHighlight'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { createTestStore } from './helpers'

type Opts = Parameters<typeof useTurnRecovery>[0]

function harness() {
  const store = createTestStore()
  const opts: Opts = {
    activeSlot: 'slot-a',
    slotRunning: false,
    messages: [],
    navigate: vi.fn(),
    anchorModelBtn: vi.fn(),
    setModelDropdown: vi.fn(),
    modelPickerReturnsFocusRef: { current: true },
    showRefusedPress: vi.fn(),
  }
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook((p: Opts) => useTurnRecovery(p), { initialProps: opts, wrapper })
  return { opts, hook }
}

afterEach(() => { document.body.replaceChildren() })

describe('error-row fixes', () => {
  it('opens the model picker on the composer chip, with no focus return', () => {
    const chip = document.createElement('button')
    chip.dataset.testid = 'composer-model-chip'
    document.body.append(chip)
    const rect = new DOMRect(40, 500, 120, 24)
    vi.spyOn(chip, 'getBoundingClientRect').mockReturnValue(rect)
    const { opts, hook } = harness()
    act(() => { hook.result.current.openModelPickerFromError() })
    expect(opts.anchorModelBtn).toHaveBeenCalledWith(rect, chip)
    expect(opts.modelPickerReturnsFocusRef.current).toBe(false)
    expect(opts.setModelDropdown).toHaveBeenCalledWith(true)
  })

  it('anchors the picker to the composer edge when the chip is not on screen', () => {
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 800 })
    const { opts, hook } = harness()
    act(() => { hook.result.current.openModelPickerFromError() })
    const [rect, trigger] = (opts.anchorModelBtn as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(trigger).toBeNull()
    expect({ x: rect.x, y: rect.y, width: rect.width, height: rect.height }).toEqual({ x: 16, y: 704, width: 160, height: 28 })
    expect(opts.setModelDropdown).toHaveBeenCalledWith(true)
  })

  it('routes each link fix to the page its row names', () => {
    const { opts, hook } = harness()
    act(() => { hook.result.current.openDefaultModelSetting() })
    act(() => { hook.result.current.openKiroSignIn() })
    act(() => { hook.result.current.openMemberCapabilities('Ops & QA') })
    expect((opts.navigate as ReturnType<typeof vi.fn>).mock.calls).toEqual([
      [settingsPath({ tab: 'chat', sub: 'models', highlight: SETTINGS_DEFAULT_MODEL_ID })],
      [KIRO_SIGN_IN_PATH],
      ['/capabilities?tab=crews&crew=Ops%20%26%20QA&pane=capabilities'],
    ])
  })
})
