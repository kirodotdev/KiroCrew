// Feature: chat-tool-row — the shell activity line waits for a LONG command.
//
// "Running · Ns" under a shell pill is for the command a reader is actually
// waiting on. Most shell calls return well under a second, and a line under
// every one of them is noise — for a call that ends before the clock's first
// one-second tick it would read a meaningless "0s". So the line appears only
// once the command has run for SHELL_ACTIVITY_MIN_SECS, and is removed the
// moment the command ends. Short calls add no line and remove none, so the
// bottom-pinned transcript is not stepped at every tool boundary.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import { sseToolResult } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const NOW_MS = 1_700_000_000_000
const THRESHOLD_MS = 10_000

afterEach(() => {
  vi.useRealTimers()
})

/** Pin the clock BEFORE render: `activityNow` seeds from `Date.now()` in a
 *  useState initializer, so a clock set afterwards would leave the first paint
 *  computed against the real time. */
function freezeClock() {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_MS)
}

function shellMsg(): ChatMessage {
  return { role: 'tool', content: '🔧 Running: sleep 30 && echo done', cls: '', meta: { tool_call_id: 'tc_long' } }
}

/** A live shell tool whose log entry says it STARTED `startedAgoMs` ago. */
function runningStore(startedAgoMs: number) {
  return createTestStore({
    chat: {
      messages: [shellMsg()],
      activeSlot: 'S',
      toolLog: [{ type: 'tool', text: 'sleep 30 && echo done', tool_call_id: 'tc_long', is_shell: true, ts: NOW_MS - startedAgoMs }],
      slotRunning: true,
    } as unknown as ChatState,
  })
}

describe('ToolCallLine — shell activity line threshold', () => {
  it('shows nothing for a shell command that has run under the threshold', () => {
    freezeClock()
    const store = runningStore(3_000)
    renderWithProviders(<ToolCallLine message={shellMsg()} running={true} />, { store })
    expect(screen.queryByTestId('shell-activity')).toBeNull()
  })

  it('appears once the running command crosses the threshold, with the live elapsed total', () => {
    freezeClock()
    const store = runningStore(0)
    renderWithProviders(<ToolCallLine message={shellMsg()} running={true} />, { store })
    expect(screen.queryByTestId('shell-activity')).toBeNull()

    // One second short: still nothing.
    act(() => { vi.advanceTimersByTime(THRESHOLD_MS - 1_000) })
    expect(screen.queryByTestId('shell-activity')).toBeNull()

    // The tick that reaches the threshold brings the line in, already counting.
    act(() => { vi.advanceTimersByTime(1_000) })
    expect(screen.getByTestId('shell-activity')).toBeTruthy()
    expect(screen.getByText(/Running · 10s/)).toBeTruthy()

    act(() => { vi.advanceTimersByTime(2_000) })
    expect(screen.getByText(/Running · 12s/)).toBeTruthy()
  })

  it('shows the line at once when the row mounts mid-command already past the threshold', () => {
    // The clock is anchored to the tool's own start, not to this mount: a row
    // the virtualizer re-mounts (or a reload) 25s into a command must not
    // make the reader wait another ten ticks.
    freezeClock()
    const store = runningStore(25_000)
    renderWithProviders(<ToolCallLine message={shellMsg()} running={true} />, { store })
    expect(screen.getByText(/Running · 25s/)).toBeTruthy()
  })

  it('removes the line when the command completes', async () => {
    // Real clock here: the exit eases the row's height to zero on animation
    // frames before unmounting, and those frames must actually run.
    const store = createTestStore({
      chat: {
        messages: [shellMsg()],
        activeSlot: 'S',
        toolLog: [{ type: 'tool', text: 'sleep 30 && echo done', tool_call_id: 'tc_long', is_shell: true, ts: Date.now() - 15_000 }],
        slotRunning: true,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={shellMsg()} running={true} />, { store })
    expect(screen.getByTestId('shell-activity')).toBeTruthy()

    act(() => {
      store.dispatch(sseToolResult({ slot: 'S', tool_call_id: 'tc_long', output: 'done' }))
    })
    await waitFor(() => expect(screen.queryByTestId('shell-activity')).toBeNull())
  })

  it('never shows the line for a tool that was ALREADY done at mount', () => {
    // A historical row (reload, scrollback) starts done: the line is a live
    // indicator and has nothing to indicate for a finished command, however
    // long ago it started.
    const store = createTestStore({
      chat: {
        messages: [shellMsg()],
        activeSlot: 'S',
        toolLog: [{ type: 'tool', text: 'sleep 30 && echo done', tool_call_id: 'tc_long', is_shell: true, output: 'done', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={shellMsg()} running={false} />, { store })
    expect(screen.queryByTestId('shell-activity')).toBeNull()
  })
})
