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
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { ApiError } from '../api/apiError'
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
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}><Provider store={store}>{children}</Provider></QueryClientProvider>
  )
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
    act(() => { hook.result.current.openMemberCapabilities('Ops & QA', 'member') })
    expect((opts.navigate as ReturnType<typeof vi.fn>).mock.calls).toEqual([
      [settingsPath({ tab: 'chat', sub: 'models', highlight: SETTINGS_DEFAULT_MODEL_ID })],
      [KIRO_SIGN_IN_PATH],
      ['/capabilities?tab=crews&crew=Ops%20%26%20QA&pane=capabilities'],
      ['/capabilities?tab=crews&crew=Ops%20%26%20QA'],
    ])
  })
})

describe('fresh session on a context overflow', () => {
  const overflowRows = [
    { role: 'user', content: 'hi' },
    { role: 'error', content: 'Input is too long', meta: { kind: 'context_overflow' } },
  ] as unknown as Opts['messages']

  afterEach(() => { vi.restoreAllMocks() })

  it('resets the active slot with replay and reports done', async () => {
    const reset = vi.spyOn(api, 'resetConversation').mockResolvedValue({ slot: 'slot-a', reset: true, replay: true })
    const { hook } = harness()
    expect(hook.result.current.freshSession).toBe('idle')
    await act(async () => { hook.result.current.handleFreshSession() })
    expect(reset).toHaveBeenCalledTimes(1)
    expect(reset).toHaveBeenCalledWith('slot-a')
    expect(hook.result.current.freshSession).toBe('done')
  })

  it('shows a refusal and goes back to idle when the server says no', async () => {
    const err = new Error('a turn is in flight')
    vi.spyOn(api, 'resetConversation').mockRejectedValue(err)
    const { opts, hook } = harness()
    await act(async () => { hook.result.current.handleFreshSession() })
    expect(opts.showRefusedPress).toHaveBeenCalledWith('fresh_session', err)
    expect(hook.result.current.freshSession).toBe('idle')
  })

  it('does nothing while a turn runs, and the done state is per slot', async () => {
    const reset = vi.spyOn(api, 'resetConversation').mockResolvedValue({ slot: 'slot-a', reset: true, replay: true })
    const { opts, hook } = harness()
    hook.rerender({ ...opts, slotRunning: true })
    await act(async () => { hook.result.current.handleFreshSession() })
    expect(reset).not.toHaveBeenCalled()
    hook.rerender({ ...opts, slotRunning: false })
    await act(async () => { hook.result.current.handleFreshSession() })
    expect(hook.result.current.freshSession).toBe('done')
    hook.rerender({ ...opts, activeSlot: 'slot-b' })
    expect(hook.result.current.freshSession).toBe('idle')
  })

  it('a request that settles after a tab switch writes only its own slot', async () => {
    let settleA: (v: { slot: string; reset: boolean; replay: boolean }) => void = () => {}
    vi.spyOn(api, 'resetConversation')
      .mockImplementationOnce(() => new Promise(res => { settleA = res }))
      .mockImplementationOnce(() => new Promise(() => {}))
    const { opts, hook } = harness()
    act(() => { hook.result.current.handleFreshSession() })
    hook.rerender({ ...opts, activeSlot: 'slot-b' })
    act(() => { hook.result.current.handleFreshSession() })
    expect(hook.result.current.freshSession).toBe('pending')
    await act(async () => { settleA({ slot: 'slot-a', reset: true, replay: true }) })
    expect(hook.result.current.freshSession).toBe('pending')
    hook.rerender({ ...opts, activeSlot: 'slot-a' })
    expect(hook.result.current.freshSession).toBe('done')
  })

  it('withholds the composer Resume while the newest row is an overflow', () => {
    const { opts, hook } = harness()
    expect(hook.result.current.cardOwnsRecovery).toBe(false)
    hook.rerender({ ...opts, messages: overflowRows })
    expect(hook.result.current.cardOwnsRecovery).toBe(true)
  })

  for (const code of ['parent_identity_changed', 'foreign_private_copy', 'project_identity_changed', 'private_template_shadowed']) {
    it(`withholds the composer Resume while the newest row is ${code}`, () => {
      const { opts, hook } = harness()
      const rows = [
        { role: 'user', content: 'hi', ts: 1 },
        { role: 'error', content: `${code}: x`, ts: 2, meta: { code, member: 'reviewer' } },
      ] as Opts['messages']
      hook.rerender({ ...opts, messages: rows })
      expect(hook.result.current.cardOwnsRecovery).toBe(true)
    })
  }
})

describe('repair images on a many-image pixel-cap wedge', () => {
  const coded = (code: string, message = 'x') => new ApiError(409, message, JSON.stringify({ code, error: message }))

  afterEach(() => { vi.restoreAllMocks() })

  it('repairs, re-issues, and reports done when the turn resumed', async () => {
    const repair = vi.spyOn(api, 'repairImages').mockResolvedValue({ slot: 'slot-a', repaired: true, resumed: true })
    const { hook } = harness()
    expect(hook.result.current.repairImages).toBe('idle')
    await act(async () => { hook.result.current.handleRepairImages() })
    expect(repair).toHaveBeenCalledWith('slot-a')
    expect(hook.result.current.repairImages).toBe('done')
  })

  it('reports done_not_resumed when the repair landed but the re-issue raced', async () => {
    vi.spyOn(api, 'repairImages').mockResolvedValue({ slot: 'slot-a', repaired: true, resumed: false })
    const { hook } = harness()
    await act(async () => { hook.result.current.handleRepairImages() })
    expect(hook.result.current.repairImages).toBe('done_not_resumed')
  })

  it('stays idle when nothing was over the cap (repaired=false), re-issuing the turn', async () => {
    vi.spyOn(api, 'repairImages').mockResolvedValue({ slot: 'slot-a', repaired: false, resumed: false })
    const { hook } = harness()
    await act(async () => { hook.result.current.handleRepairImages() })
    expect(hook.result.current.repairImages).toBe('idle')
  })

  it('goes to the failed dead-end on image_repair_backup_exists, suppressing the top notice', async () => {
    vi.spyOn(api, 'repairImages').mockRejectedValue(coded('image_repair_backup_exists', 'a .pre-image-repair.bak sidecar already exists'))
    const { opts, hook } = harness()
    await act(async () => { hook.result.current.handleRepairImages() })
    // The failed card carries its own reason, so no refused-press notice on top.
    expect(opts.showRefusedPress).not.toHaveBeenCalled()
    expect(hook.result.current.repairImages).toBe('failed')
  })

  it('stays retryable on image_repair_failed, showing the friendly localized hint', async () => {
    vi.spyOn(api, 'repairImages').mockRejectedValue(coded('image_repair_failed', 'the transcript moved under the repair'))
    const { opts, hook } = harness()
    await act(async () => { hook.result.current.handleRepairImages() })
    // Generic repair failure: the friendly hint, NOT the raw backend text.
    const [action, err] = (opts.showRefusedPress as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(action).toBe('repair_images')
    expect((err as Error).message).not.toContain('transcript moved')
    expect(hook.result.current.repairImages).toBe('idle')
  })

  it('surfaces the specific reason for a transient reload 409 instead of the generic hint', async () => {
    vi.spyOn(api, 'repairImages').mockRejectedValue(coded('turn_in_flight', 'a turn is already in flight on this slot'))
    const { opts, hook } = harness()
    await act(async () => { hook.result.current.handleRepairImages() })
    const [action, err] = (opts.showRefusedPress as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(action).toBe('repair_images')
    // A transient code carries an actionable reason, so it is passed through.
    expect((err as Error).message).toContain('turn is already in flight')
    expect(hook.result.current.repairImages).toBe('idle')
  })

  it('does nothing while a turn is running', async () => {
    const repair = vi.spyOn(api, 'repairImages').mockResolvedValue({ slot: 'slot-a', repaired: true, resumed: true })
    const { opts, hook } = harness()
    hook.rerender({ ...opts, slotRunning: true })
    await act(async () => { hook.result.current.handleRepairImages() })
    expect(repair).not.toHaveBeenCalled()
  })
})
