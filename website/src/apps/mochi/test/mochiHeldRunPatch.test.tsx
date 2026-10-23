/**
 * The pet's transcript takes back the word the gateway held across a tool call.
 *
 * A tool call or an approval prompt cuts the reply into rows, but the text on
 * either side of it can be the two halves of one credential. So the gateway holds
 * the last run of text before the cut off the row it commits, and once the text
 * after the cut shows the run is a word of its own, gives it back to that row as
 * a content patch (`chat_message_update`, keyed by the row's `mid`), which the
 * bridge hands the panel as a `_patch` frame. The panel gets rows from
 * `chat_message` frames and from its history load, so without the patch the
 * word never shows here.
 */
import React from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type Listener = (msg: Record<string, unknown>) => void
const listeners: { message: Listener | null } = { message: null }
// Rows the panel's history load answers. A test that sets none gets a load that
// never answers, so nothing replaces the rows it emits.
const history: { rows: Record<string, unknown>[] | null } = { rows: null }

vi.mock('../src/mochiApi', () => ({
  api: {
    onChatMessage: (cb: Listener) => {
      listeners.message = cb
      return () => {
        listeners.message = null
      }
    },
    getChatHistory: () =>
      history.rows ? Promise.resolve(history.rows) : new Promise<Record<string, unknown>[]>(() => {}),
  },
}))

vi.mock('../panel/panelBridge', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../panel/panelBridge')>()),
  reportStat: vi.fn(),
}))

import { ChatPanel } from '../src/renderer/ChatPanel'

function emit(msg: Record<string, unknown>): void {
  act(() => {
    listeners.message?.(msg)
  })
}

function assistant(mid: string, content: string): Record<string, unknown> {
  return { id: `assistant-${mid}`, role: 'assistant', content, timestamp: 1, meta: { mid } }
}

function bubbles(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.msg-bubble')).map((el) => el.textContent ?? '')
}

describe('mochi ChatPanel row patches', () => {
  beforeEach(() => {
    listeners.message = null
    history.rows = null
  })
  afterEach(cleanup)

  it('puts a word given back after a tool call on the row it was cut from', () => {
    const { container } = render(<ChatPanel />)
    emit(assistant('m1', 'Let me check the '))
    emit(assistant('m2', ' file now.'))
    emit({ _patch: 'm1', content: 'Let me check the config' })

    const shown = bubbles(container)
    expect(shown).toHaveLength(2)
    expect(shown[0]).toContain('Let me check the config')
    expect(shown[1]).toContain('file now.')
  })

  it('does not draw a row committed empty until its text is given back', () => {
    // A segment that is all one held run is committed with no text yet.
    const { container } = render(<ChatPanel />)
    emit(assistant('m1', 'Let me look.'))
    emit(assistant('m2', ''))
    expect(bubbles(container)).toHaveLength(1)

    emit({ _patch: 'm2', content: 'OK.' })
    const shown = bubbles(container)
    expect(shown).toHaveLength(2)
    expect(shown[1]).toContain('OK.')
  })

  it('puts a word given back on a row the panel loaded from history', async () => {
    // The panel opened while the gateway still held the last word of one row and
    // all the text of the next, so the history load has them short and empty.
    // The first row is an older one, stored with no row id and no timestamp.
    history.rows = [
      { role: 'user', content: 'Is the config right?' },
      { role: 'assistant', content: 'Let me check the ', timestamp: 1, meta: { mid: 'm1' } },
      { role: 'assistant', content: '', timestamp: 2, meta: { mid: 'm2' } },
    ]
    const { container } = render(<ChatPanel />)
    await act(async () => {})
    expect(bubbles(container)).toEqual([
      expect.stringContaining('Is the config right?'),
      expect.stringContaining('Let me check the'),
    ])

    emit({ _patch: 'm1', content: 'Let me check the config' })
    emit({ _patch: 'm2', content: 'OK.' })

    const shown = bubbles(container)
    expect(shown).toHaveLength(3)
    expect(shown[0]).toContain('Is the config right?')
    expect(shown[1]).toContain('Let me check the config')
    expect(shown[2]).toContain('OK.')
  })

  it('leaves every row a patch does not name as it was', () => {
    const { container } = render(<ChatPanel />)
    emit(assistant('m1', 'First'))
    emit({ _patch: 'unknown', content: 'Changed' })

    expect(bubbles(container)).toEqual([expect.stringContaining('First')])
    expect(container.textContent).not.toContain('Changed')
  })
})
