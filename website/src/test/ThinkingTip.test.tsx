import { describe, it, expect, afterEach, vi } from 'vitest'
import type { ReactNode, SetStateAction } from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '../i18n/all'
import ChatFooter from '../pages/chat/ChatFooter'
import { pickExploratoryTip, ThinkingTipController, uniformIndex, useThinkingTip, useThinkingTipEnv, type ExploratoryTip, type ThinkingTipRowFn } from '../pages/chat/ThinkingTip'
import { ThinkingTipRow, tipRuns } from '../pages/chat/ThinkingTipView'
import { api } from '../api/client'

// The phone breakpoint, switched per test.
const viewport = vi.hoisted(() => ({ phone: false }))
vi.mock('../hooks/useIsMobile', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => viewport.phone,
}))
// The recorder module the microphone check loads lazily; `fail` makes that
// load reject, as a chunk that cannot be fetched does.
const voice = vi.hoisted(() => ({ fail: false }))
vi.mock('../hooks/useVoiceInput', () => ({
  get voiceInputSupported() {
    if (voice.fail) throw new Error('chunk failed')
    return false
  },
}))
// The dashboard language, switched per test.
const lang = vi.hoisted(() => ({ resolved: 'en' }))
vi.mock('../i18n/LanguageProvider', async (importOriginal) => {
  const real = await importOriginal<typeof import('../i18n/LanguageProvider')>()
  return { ...real, useLanguage: () => ({ ...real.useLanguage(), resolved: lang.resolved }) }
})

const SIDE: ExploratoryTip = { id: 'e07-side-chat', body: '**Side question?** Type `/side` or `/btw`.', requires: [] }
const VOICE: ExploratoryTip = { id: 'e09-voice-input', body: '**Want to dictate?** Press **Voice input**.', requires: ['microphone'] }
const base = { stopping: false, state: '', lastRole: 'user' }
// ChatFooter draws the tip through the row builder ThinkingTipController
// hands up; these tests build it directly.
const tipRow = ({ tip = null, loadError = null, onShown = () => {}, onDismiss = () => {} }: {
  tip?: ExploratoryTip | null; loadError?: string | null; onShown?: () => void; onDismiss?: () => void
}) => (statusRow: ReactNode) => (
  <ThinkingTipRow statusRow={statusRow} tip={tip} loadError={loadError} onShown={onShown} onDismiss={onDismiss} dismissError={null} />
)

afterEach(() => { vi.restoreAllMocks(); viewport.phone = false; lang.resolved = 'en'; voice.fail = false })

describe('tipRuns', () => {
  it('splits bold and code runs and keeps the words', () => {
    expect(tipRuns(SIDE.body)).toEqual([
      { kind: 'bold', text: 'Side question?' },
      { kind: 'text', text: ' Type ' },
      { kind: 'code', text: '/side' },
      { kind: 'text', text: ' or ' },
      { kind: 'code', text: '/btw' },
      { kind: 'text', text: '.' },
    ])
  })
})

describe('pickExploratoryTip', () => {
  it('skips dismissed tips and tips whose requirement is not met', () => {
    const env = new Set<string>()
    expect(pickExploratoryTip([SIDE, VOICE], env, new Set(), n => n - 1)).toBe(SIDE)
    expect(pickExploratoryTip([SIDE, VOICE], env, new Set([SIDE.id]))).toBeNull()
    expect(pickExploratoryTip([SIDE, VOICE], new Set(['microphone']), new Set(), n => n - 1)).toBe(VOICE)
    expect(pickExploratoryTip([{ ...SIDE, requires: ['unknown-key'] }], new Set(['desktop']), new Set())).toBeNull()
  })
})

describe('uniformIndex', () => {
  it('redraws a value in the uneven top slice and stays in range', () => {
    const draws = [2 ** 32 - 1, 7]
    const spy = vi.spyOn(crypto, 'getRandomValues').mockImplementation(<T extends ArrayBufferView | null>(a: T): T => {
      ;(a as unknown as Uint32Array)[0] = draws.shift() ?? 0
      return a
    })
    // 2**32 - 1 is above the largest multiple of 3, so it is redrawn; 7 % 3 = 1.
    expect(uniformIndex(3)).toBe(1)
    expect(spy).toHaveBeenCalledTimes(2)
  })
})

describe('ChatFooter thinking tip', () => {
  it('shows the tip after "Thinking…" while the turn runs, and hides it when the turn ends', () => {
    const onShown = vi.fn()
    const row = tipRow({ tip: SIDE, onShown })
    const { rerender } = render(<ChatFooter {...base} running thinkingTipRow={row} />)
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
    const text = screen.getByTestId('thinking-tip-text')
    expect(text.textContent).toBe('Tip: Side question? Type /side or /btw.')
    expect(text.querySelector('strong')?.textContent).toBe('Side question?')
    expect([...text.querySelectorAll('code')].map(c => c.textContent)).toEqual(['/side', '/btw'])
    // A long tip is never cut off: the row wraps it onto its own line.
    expect(text.className).not.toContain('truncate')
    expect(screen.getByTestId('thinking-tip').parentElement?.className).toContain('flex-wrap')
    expect(onShown).toHaveBeenCalledTimes(1)
    rerender(<ChatFooter {...base} running={false} thinkingTipRow={row} />)
    expect(screen.queryByTestId('thinking-tip')).toBeNull()
  })

  it('leaves the row as it was without a tip', () => {
    render(<ChatFooter {...base} running />)
    expect(screen.queryByTestId('thinking-tip')).toBeNull()
    expect(screen.getByRole('status').parentElement?.className).toBe('py-2.5')
  })

  it('names a failed tip load beside "Thinking…" when there is no tip', () => {
    render(<ChatFooter {...base} running thinkingTipRow={tipRow({ loadError: 'HTTP 503' })} />)
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
    expect(screen.getByTestId('thinking-tip-load-error')).toBeInTheDocument()
  })

  it('names a refresh that failed beside a tip already shown', () => {
    // The tip stays from the earlier read; the later read failed.
    render(<ChatFooter {...base} running thinkingTipRow={tipRow({ tip: SIDE, loadError: 'HTTP 503' })} />)
    expect(screen.getByTestId('thinking-tip-text')).toBeInTheDocument()
    expect(screen.getByTestId('thinking-tip-load-error')).toBeInTheDocument()
  })

  it('dismisses from the hover x', () => {
    const onDismiss = vi.fn()
    render(<ChatFooter {...base} running thinkingTipRow={tipRow({ tip: SIDE, onDismiss })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss tip' }))
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})

function mockApi(opts: { optedOut?: boolean; tips?: ExploratoryTip[] } = {}) {
  const feedback = vi.spyOn(api, 'tipsFeedback').mockResolvedValue({ ok: true })
  vi.spyOn(api, 'tipsStatus').mockResolvedValue({ enabled_config: true, opted_out: !!opts.optedOut, cadence_hours: 6 })
  vi.spyOn(api, 'tipsExploratory').mockResolvedValue({ tips: opts.tips ?? [SIDE] })
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: {} } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
  const next = vi.spyOn(api, 'tipsNext')
  return { feedback, next }
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const ENV: ReadonlySet<string> = new Set(['desktop'])
// The pick waits on a chain: tipsStatus settles, then tipsExploratory is
// enabled and settles, then the turn and pick effects run. A loaded worker can
// take that past waitFor's one-second default.
const CHAIN_TIMEOUT = { timeout: 5000 }

describe('useThinkingTip', () => {
  it('picks one tip per turn, reports it shown once, and drops it at turn end', async () => {
    const { feedback, next } = mockApi()
    const { result, rerender } = renderHook(({ running }) => useThinkingTip(running, 's1', false, ENV), {
      initialProps: { running: true }, wrapper: wrapper(),
    })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    act(() => { result.current.onShown(); result.current.onShown() })
    expect(feedback).toHaveBeenCalledTimes(1)
    expect(feedback).toHaveBeenCalledWith(SIDE.id, 'shown')
    rerender({ running: false })
    expect(result.current.tip).toBeNull()
    // The card above the composer has its own fetch; this row never asks for it.
    expect(next).not.toHaveBeenCalled()
  })

  it('does not show a dismissed tip again', async () => {
    const { feedback } = mockApi()
    const { result, rerender } = renderHook(({ running }) => useThinkingTip(running, 's1', false, ENV), {
      initialProps: { running: true }, wrapper: wrapper(),
    })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    await act(async () => { result.current.dismiss() })
    expect(feedback).toHaveBeenCalledWith(SIDE.id, 'dismiss')
    expect(result.current.tip).toBeNull()
    rerender({ running: false })
    rerender({ running: true })
    await act(async () => {})
    expect(result.current.tip).toBeNull()
  })

  it('hides the tip, and picks no other, when the screen loses its control mid-turn', async () => {
    mockApi({ tips: [VOICE, SIDE] })
    // Draw 0: the first eligible tip, VOICE.
    vi.spyOn(crypto, 'getRandomValues').mockImplementation(<T extends ArrayBufferView | null>(a: T): T => {
      ;(a as unknown as Uint32Array)[0] = 0
      return a
    })
    const withMic: ReadonlySet<string> = new Set(['desktop', 'microphone'])
    const { result, rerender } = renderHook(({ env }) => useThinkingTip(true, 's1', false, env), {
      initialProps: { env: withMic }, wrapper: wrapper(),
    })
    await waitFor(() => expect(result.current.tip?.id).toBe(VOICE.id), CHAIN_TIMEOUT)
    rerender({ env: ENV })
    await act(async () => {})
    expect(result.current.tip).toBeNull()
  })

  // Turn ids: `running` flips with a local send before this turn's own id
  // arrives, so the slot still carries the id it had while idle.
  const renderTurns = (initial: { running: boolean; turnId: string }) =>
    renderHook(({ running, turnId }) => useThinkingTip(running, 's1', false, ENV, turnId), {
      initialProps: initial, wrapper: wrapper(),
    })
  const shownCount = (feedback: ReturnType<typeof mockApi>['feedback']) =>
    feedback.mock.calls.filter(c => c[1] === 'shown').length

  it('keeps one pick and one shown event when the turn id arrives after the send', async () => {
    const { feedback } = mockApi()
    const { result, rerender } = renderTurns({ running: false, turnId: 'g:1' })
    // The bank is loaded before the send, so only the turn rule holds the pick.
    await waitFor(() => expect(api.tipsExploratory).toHaveBeenCalled(), CHAIN_TIMEOUT)
    await act(async () => {})
    rerender({ running: true, turnId: 'g:1' })
    await act(async () => {})
    // Still the last turn's id: no pick for it.
    expect(result.current.tip).toBeNull()
    rerender({ running: true, turnId: 'g:2' })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    act(() => { result.current.onShown() })
    rerender({ running: true, turnId: 'g:2' })
    await act(async () => {})
    act(() => { result.current.onShown() })
    expect(shownCount(feedback)).toBe(1)
  })

  it('gives a queued next turn of the same chat a fresh pick and a new shown event', async () => {
    const { feedback } = mockApi()
    const { result, rerender } = renderTurns({ running: false, turnId: 'g:1' })
    await waitFor(() => expect(api.tipsStatus).toHaveBeenCalled(), CHAIN_TIMEOUT)
    rerender({ running: true, turnId: 'g:1' })
    rerender({ running: true, turnId: 'g:2' })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    act(() => { result.current.onShown() })
    // The queued send starts the next turn without the chat ever going idle.
    rerender({ running: true, turnId: 'g:3' })
    await act(async () => {})
    act(() => { result.current.onShown() })
    expect(result.current.tip?.id).toBe(SIDE.id)
    expect(shownCount(feedback)).toBe(2)
  })

  it('picks once for a turn started elsewhere, then again for its queued next turn', async () => {
    const { feedback } = mockApi()
    const { result, rerender } = renderTurns({ running: false, turnId: 'g:1' })
    await waitFor(() => expect(api.tipsStatus).toHaveBeenCalled(), CHAIN_TIMEOUT)
    // Another tab starts the chat: running and the new id arrive together.
    rerender({ running: true, turnId: 'g:2' })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    act(() => { result.current.onShown() })
    rerender({ running: true, turnId: 'g:3' })
    await act(async () => {})
    act(() => { result.current.onShown() })
    expect(shownCount(feedback)).toBe(2)
  })

  it('treats the first id change as the next turn when it opens on a chat already running', async () => {
    const { feedback } = mockApi()
    const { result, rerender } = renderTurns({ running: true, turnId: 'g:5' })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    act(() => { result.current.onShown() })
    rerender({ running: true, turnId: 'g:6' })
    await act(async () => {})
    act(() => { result.current.onShown() })
    expect(result.current.tip?.id).toBe(SIDE.id)
    expect(shownCount(feedback)).toBe(2)
  })

  it('names a shown report that failed beside the tip and sends it again on the next mount', async () => {
    const { feedback } = mockApi()
    feedback.mockRejectedValueOnce(new Error('gateway unreachable'))
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    await act(async () => { result.current.onShown() })
    expect(result.current.dismissError).toBe('gateway unreachable')
    await act(async () => { result.current.onShown() })
    expect(shownCount(feedback)).toBe(2)
    expect(result.current.dismissError).toBeNull()
  })

  it('names a recorder chunk that failed to load, instead of silently dropping the Voice input tip', async () => {
    mockApi({ tips: [] })
    voice.fail = true
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.loadError).toBe('chunk failed'), CHAIN_TIMEOUT)
    expect(result.current.tip).toBeNull()
  })

  it('names a failed config read beside the tip', async () => {
    mockApi()
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('HTTP 500'))
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    await waitFor(() => expect(result.current.loadError).toBe('HTTP 500'), CHAIN_TIMEOUT)
  })

  it('shows no tip when the dashboard language is not English', async () => {
    const { feedback } = mockApi()
    lang.resolved = 'zh-CN'
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(api.tipsStatus).toHaveBeenCalled(), CHAIN_TIMEOUT)
    await act(async () => {})
    expect(result.current.tip).toBeNull()
    expect(api.tipsExploratory).not.toHaveBeenCalled()
    expect(feedback).not.toHaveBeenCalled()
  })

  it('hides a dismissed tip even after a switch to another running chat before the reply', async () => {
    const { feedback } = mockApi()
    let resolve: (v: { ok: true }) => void = () => {}
    feedback.mockImplementation((_id, action) => action === 'dismiss'
      ? new Promise(r => { resolve = r })
      : Promise.resolve({ ok: true }))
    const { result, rerender } = renderHook(({ slot }) => useThinkingTip(true, slot, false, ENV), {
      initialProps: { slot: 's1' }, wrapper: wrapper(),
    })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    act(() => { result.current.dismiss() })
    rerender({ slot: 's2' })
    // The only tip is picked again for the new chat's turn.
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    await act(async () => { resolve({ ok: true }) })
    expect(result.current.tip).toBeNull()
  })

  it('keeps the tip and names the failure when the dismiss is not saved', async () => {
    const { feedback } = mockApi()
    feedback.mockRejectedValueOnce(new Error('gateway unreachable'))
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    await act(async () => { result.current.dismiss() })
    expect(result.current.tip?.id).toBe(SIDE.id)
    expect(result.current.dismissError).toBe('gateway unreachable')
  })

  it('drops a failed dismiss notice when the turn ends', async () => {
    const { feedback } = mockApi()
    feedback.mockRejectedValueOnce(new Error('gateway unreachable'))
    const { result, rerender } = renderHook(({ running }) => useThinkingTip(running, 's1', false, ENV), {
      initialProps: { running: true }, wrapper: wrapper(),
    })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    await act(async () => { result.current.dismiss() })
    expect(result.current.dismissError).toBe('gateway unreachable')
    rerender({ running: false })
    rerender({ running: true })
    await waitFor(() => expect(result.current.tip?.id).toBe(SIDE.id), CHAIN_TIMEOUT)
    expect(result.current.dismissError).toBeNull()
  })

  it('names a failed load while the turn runs, but not a gateway without the route', async () => {
    mockApi()
    vi.mocked(api.tipsExploratory).mockRejectedValue(new Error('HTTP 503'))
    const failed = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(failed.result.current.loadError).toBe('HTTP 503'), CHAIN_TIMEOUT)
    expect(failed.result.current.tip).toBeNull()
    vi.mocked(api.tipsExploratory).mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
    const old = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await waitFor(() => expect(api.tipsExploratory).toHaveBeenCalledTimes(2), CHAIN_TIMEOUT)
    await act(async () => {})
    expect(old.result.current.loadError).toBeNull()
  })

  it('drops a failed tip load once tips are switched off', async () => {
    mockApi()
    vi.mocked(api.tipsExploratory).mockRejectedValue(new Error('HTTP 503'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrap = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrap })
    await waitFor(() => expect(result.current.loadError).toBe('HTTP 503'), CHAIN_TIMEOUT)
    // What Settings writes when the user turns Feature Tips off.
    act(() => { qc.setQueryData(['tipsStatus'], { enabled_config: true, opted_out: true, cadence_hours: 6 }) })
    await waitFor(() => expect(result.current.loadError).toBeNull(), CHAIN_TIMEOUT)
  })

  it('shows nothing when tips are switched off', async () => {
    const { feedback } = mockApi({ optedOut: true })
    const fetchTips = vi.mocked(api.tipsExploratory)
    const { result } = renderHook(() => useThinkingTip(true, 's1', false, ENV), { wrapper: wrapper() })
    await act(async () => {})
    expect(result.current.tip).toBeNull()
    expect(fetchTips).not.toHaveBeenCalled()
    expect(feedback).not.toHaveBeenCalled()
  })

  it('shows nothing while blocked', async () => {
    mockApi()
    const { result } = renderHook(() => useThinkingTip(true, 's1', true, ENV), { wrapper: wrapper() })
    await act(async () => {})
    expect(result.current.tip).toBeNull()
    expect(api.tipsExploratory).not.toHaveBeenCalled()
  })
})

describe('ThinkingTipController', () => {
  it('hands the running turn\'s tip up, and none once the turn ends', async () => {
    mockApi()
    let row: ThinkingTipRowFn | null = null
    const onChange = (next: SetStateAction<ThinkingTipRowFn | null>) => { row = typeof next === 'function' ? next(row) : next }
    const Wrap = wrapper()
    const props = { slotKey: 's1', localKey: 's1', blocked: false, turnId: null, projectDir: null, splitSend: true, onChange }
    const { rerender } = render(<Wrap><ThinkingTipController running {...props} /></Wrap>)
    await waitFor(() => expect(row).not.toBeNull(), CHAIN_TIMEOUT)
    const drawn = render(<>{row!(<span>Thinking…</span>)}</>)
    expect(drawn.getByTestId('thinking-tip-text').textContent).toContain('Side question?')
    drawn.unmount()
    rerender(<Wrap><ThinkingTipController running={false} {...props} /></Wrap>)
    await waitFor(() => expect(row).toBeNull())
  })
})

describe('useThinkingTipEnv', () => {
  it('offers the Steer / Queue tip only where the composer has the split send menu', () => {
    const run = (splitSend: boolean) => renderHook(
      () => useThinkingTipEnv({ projectDir: null, localSession: true, splitSend }), { wrapper: wrapper() },
    ).result.current
    expect(run(true).has('split_send')).toBe(true)
    // A crew worker's composer is steer-only.
    expect(run(false).has('split_send')).toBe(false)
  })

  it('offers the shell command tip only in a chat that runs on this machine', () => {
    const run = (localSession: boolean) => renderHook(
      () => useThinkingTipEnv({ projectDir: null, localSession, splitSend: true }), { wrapper: wrapper() },
    ).result.current
    expect(run(true).has('local_session')).toBe(true)
    expect(run(false).has('local_session')).toBe(false)
  })
})

/** Give every element the same layout widths for one test: the tip's natural
 *  width and the width of the line it sits on. */
function stubWidths(natural: number, line: number) {
  const offset = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
  const client = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => natural })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => line })
  return () => {
    if (offset) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', offset)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    if (client) Object.defineProperty(HTMLElement.prototype, 'clientWidth', client)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
  }
}

describe('thinking tip on a phone', () => {
  it('takes its own full-width line under "Thinking…", on one line, with no divider', () => {
    viewport.phone = true
    const restore = stubWidths(280, 360)
    try {
      const onShown = vi.fn()
      render(<ChatFooter {...base} running thinkingTipRow={tipRow({ tip: SIDE, onShown })} />)
      const tip = screen.getByTestId('thinking-tip')
      expect(tip.dataset.fits).toBe('true')
      expect(tip.className).toContain('basis-full')
      expect(tip.querySelector('.w-px')).toBeNull()
      expect(screen.getByTestId('thinking-tip-text').className).toContain('whitespace-nowrap')
      expect(screen.getByRole('button', { name: 'Dismiss tip' })).toBeInTheDocument()
      expect(onShown).toHaveBeenCalledTimes(1)
    } finally {
      restore()
    }
  })

  it('shows nothing, and does not count it shown, when the tip would not fit on one line', () => {
    viewport.phone = true
    const restore = stubWidths(500, 360)
    try {
      const onShown = vi.fn()
      render(<ChatFooter {...base} running thinkingTipRow={tipRow({ tip: SIDE, onShown })} />)
      const tip = screen.getByTestId('thinking-tip')
      expect(tip.dataset.fits).toBe('false')
      expect(tip.firstElementChild?.className).toContain('invisible')
      expect(screen.queryByRole('button', { name: 'Dismiss tip' })).toBeNull()
      expect(onShown).not.toHaveBeenCalled()
      expect(screen.getByText('Thinking…')).toBeInTheDocument()
    } finally {
      restore()
    }
  })

  it('offers the tips a phone can use and leaves out the desktop-only ones', () => {
    viewport.phone = true
    const env = renderHook(
      () => useThinkingTipEnv({ projectDir: null, localSession: true, splitSend: true }), { wrapper: wrapper() },
    ).result.current
    expect(env.has('desktop')).toBe(false)
    expect(env.has('split_send')).toBe(true)
    const desktopOnly: ExploratoryTip = { id: 'e01-split-view', body: 'x', requires: ['desktop'] }
    expect(pickExploratoryTip([desktopOnly, SIDE], env, new Set(), () => 0)).toBe(SIDE)
  })
})
