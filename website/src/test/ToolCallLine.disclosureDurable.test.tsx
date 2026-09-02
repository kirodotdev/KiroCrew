/**
 * The per-tool-call pill's expanded panel must survive the virtualizer
 * recycling its row.
 *
 * The transcript is virtualised, so a row is unmounted once it leaves the
 * mounted window, which streaming does routinely as it scrolls content past.
 * Expansion therefore cannot live in the pill. These tests mirror ChatPage's
 * arrangement, where the host holds it keyed by the pill's message key.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { useCallback, useState } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
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

beforeEach(() => { localStorage.clear() })

const KEY = 'row-tc_1'
const toolMsg = (): ChatMessage => ({
  role: 'tool', content: '🔧 Running: echo hello', cls: '',
  meta: { tool_call_id: 'tc_1', purpose: 'Say hello' },
})

const store = () => createTestStore({
  chat: {
    messages: [toolMsg()],
    toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'hello', ts: 1 }],
    slotRunning: false,
  } as unknown as ChatState,
})

/**
 * Mirrors ChatPage: disclosure is host-owned and keyed, and the pill is only
 * rendered while its row is mounted. The recycle button stands in for the row
 * leaving the virtualizer's window and coming back.
 */
function Host() {
  const [disclosure, setDisclosure] = useState<Record<string, boolean>>({})
  const [mounted, setMounted] = useState(true)
  const setFor = useCallback((key: string, expanded: boolean) => {
    setDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  return (
    <>
      <button data-testid="recycle" onClick={() => setMounted(m => !m)}>recycle</button>
      {mounted && (
        <ToolCallLine
          message={toolMsg()}
          running={false}
          disclosure={disclosure[KEY]}
          disclosureKey={KEY}
          onDisclosureChange={setFor}
        />
      )}
    </>
  )
}

/** The pill's own disclosure button, which is the one carrying aria-expanded. */
const pill = () => screen.getAllByRole('button').find(b => b.hasAttribute('aria-expanded'))!
const recycle = () => fireEvent.click(screen.getByTestId('recycle'))

describe('ToolCallLine disclosure survives virtualizer recycling', () => {
  it('keeps the panel open when the row unmounts and mounts again', () => {
    renderWithProviders(<Host />, { store: store() })
    expect(pill().getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(pill())
    expect(pill().getAttribute('aria-expanded')).toBe('true')

    recycle()   // row leaves the mounted window
    expect(screen.getAllByRole('button').some(b => b.hasAttribute('aria-expanded'))).toBe(false)
    recycle()   // user scrolls back to it

    expect(pill().getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps the panel closed across recycling when it was never opened', () => {
    renderWithProviders(<Host />, { store: store() })
    recycle()
    recycle()
    // No entry was recorded, so the default still applies.
    expect(pill().getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps an explicit re-collapse across recycling', () => {
    renderWithProviders(<Host />, { store: store() })
    fireEvent.click(pill())   // open
    fireEvent.click(pill())   // close again
    expect(pill().getAttribute('aria-expanded')).toBe('false')

    recycle()
    recycle()

    expect(pill().getAttribute('aria-expanded')).toBe('false')
  })
})
