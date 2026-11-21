import { describe, it, expect, afterEach, vi } from 'vitest'
import type { ReactNode } from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '../i18n/all'
import ChatFooter from '../pages/chat/ChatFooter'
import { pickExploratoryTip, tipRuns, useThinkingTip, useThinkingTipEnv, type ExploratoryTip } from '../pages/chat/ThinkingTip'
import { api } from '../api/client'

const SIDE: ExploratoryTip = { id: 'e07-side-chat', body: '**Side question?** Type `/side` or `/btw`.', requires: [] }
const VOICE: ExploratoryTip = { id: 'e09-voice-input', body: '**Want to dictate?** Press **Voice input**.', requires: ['microphone'] }
const base = { stopping: false, state: '', lastRole: 'user' }

afterEach(() => vi.restoreAllMocks())

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
    expect(pickExploratoryTip([SIDE, VOICE], env, new Set(), () => 0.99)).toBe(SIDE)
    expect(pickExploratoryTip([SIDE, VOICE], env, new Set([SIDE.id]))).toBeNull()
    expect(pickExploratoryTip([SIDE, VOICE], new Set(['microphone']), new Set(), () => 0.99)).toBe(VOICE)
    expect(pickExploratoryTip([{ ...SIDE, requires: ['unknown-key'] }], new Set(['desktop']), new Set())).toBeNull()
  })
})

describe('ChatFooter thinking tip', () => {
  it('shows the tip after "Thinking…" while the turn runs, and hides it when the turn ends', () => {
    const onShown = vi.fn()
    const { rerender } = render(<ChatFooter {...base} running thinkingTip={SIDE} onThinkingTipShown={onShown} />)
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
    const text = screen.getByTestId('thinking-tip-text')
    expect(text.textContent).toBe('Tip: Side question? Type /side or /btw.')
    expect(text.querySelector('strong')?.textContent).toBe('Side question?')
    expect([...text.querySelectorAll('code')].map(c => c.textContent)).toEqual(['/side', '/btw'])
    // A long tip is never cut off: the row wraps it onto its own line.
    expect(text.className).not.toContain('truncate')
    expect(screen.getByTestId('thinking-tip').parentElement?.className).toContain('flex-wrap')
    expect(onShown).toHaveBeenCalledTimes(1)
    rerender(<ChatFooter {...base} running={false} thinkingTip={SIDE} onThinkingTipShown={onShown} />)
    expect(screen.queryByTestId('thinking-tip')).toBeNull()
  })

  it('leaves the row as it was without a tip', () => {
    render(<ChatFooter {...base} running />)
    expect(screen.queryByTestId('thinking-tip')).toBeNull()
    expect(screen.getByRole('status').parentElement?.className).toBe('py-2.5')
  })

  it('names a failed tip load beside "Thinking…" when there is no tip', () => {
    render(<ChatFooter {...base} running thinkingTipLoadError="HTTP 503" />)
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
    expect(screen.getByTestId('thinking-tip-load-error')).toBeInTheDocument()
  })

  it('dismisses from the hover x', () => {
    const onDismiss = vi.fn()
    render(<ChatFooter {...base} running thinkingTip={SIDE} onThinkingTipDismiss={onDismiss} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss tip' }))
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})

function mockApi(opts: { optedOut?: boolean; tips?: ExploratoryTip[] } = {}) {
  const feedback = vi.spyOn(api, 'tipsFeedback').mockResolvedValue({ ok: true })
  vi.spyOn(api, 'tipsStatus').mockResolvedValue({ enabled_config: true, opted_out: !!opts.optedOut, cadence_hours: 6 })
  vi.spyOn(api, 'tipsExploratory').mockResolvedValue({ tips: opts.tips ?? [SIDE] })
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
