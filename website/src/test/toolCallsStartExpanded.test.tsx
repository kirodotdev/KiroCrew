/**
 * Settings > Chat > "Tool calls start expanded" (#18254).
 *
 * The stored chat setting feeds the initial disclosure of the per-call pill
 * (ToolCallLine) and of the group row that wraps several calls
 * (CollapsibleToolGroup). Default off, so a client with no stored choice keeps
 * today's collapsed rows. A row the user toggled by hand keeps that choice.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useCallback, useState } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { RowDisclosureProvider } from '../pages/chat/rowDisclosure'
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

const setStored = (cfg: Record<string, unknown>) => localStorage.setItem('mc-chat-config', JSON.stringify(cfg))

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

/** Host-owned, keyed disclosure, as ChatPage holds it; `recycle` remounts the pill. */
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
        <ToolCallLine message={toolMsg()} running={false} disclosure={disclosure[KEY]} disclosureKey={KEY} onDisclosureChange={setFor} />
      )}
    </>
  )
}

const pill = () => screen.getAllByRole('button').find(b => b.hasAttribute('aria-expanded'))!
const groupHeader = (c: HTMLElement) => c.querySelector<HTMLButtonElement>('button[aria-expanded]')!

describe('chat config: toolCallsStartExpanded', () => {
  it('defaults to off', () => {
    expect(loadChatConfig().toolCallsStartExpanded).toBe(false)
  })

  it('coerces a stored non-boolean to off', () => {
    setStored({ toolCallsStartExpanded: 'yes' })
    expect(loadChatConfig().toolCallsStartExpanded).toBe(false)
  })
})

describe('ToolCallLine honours "Tool calls start expanded"', () => {
  it('starts collapsed when the setting is off', () => {
    renderWithProviders(<Host />, { store: store() })
    expect(pill().getAttribute('aria-expanded')).toBe('false')
  })

  it('starts expanded when the setting is on', () => {
    setStored({ toolCallsStartExpanded: true })
    renderWithProviders(<Host />, { store: store() })
    expect(pill().getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps a row the user closed by hand closed across recycling', () => {
    setStored({ toolCallsStartExpanded: true })
    renderWithProviders(<Host />, { store: store() })
    fireEvent.click(pill())
    expect(pill().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByTestId('recycle'))
    fireEvent.click(screen.getByTestId('recycle'))
    expect(pill().getAttribute('aria-expanded')).toBe('false')
  })
})

describe('CollapsibleToolGroup honours "Tool calls start expanded"', () => {
  it('starts collapsed when the setting is off', () => {
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('false')
  })

  it('starts expanded when the setting is on', () => {
    setStored({ toolCallsStartExpanded: true })
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('row')).toBeTruthy()
  })

  it('stays expanded when the group finishes running', () => {
    setStored({ toolCallsStartExpanded: true })
    const { container, rerender } = render(<CollapsibleToolGroup count={2} isRunning autoExpand><div>row</div></CollapsibleToolGroup>)
    rerender(<CollapsibleToolGroup count={2} isRunning={false} autoExpand={false}><div>row</div></CollapsibleToolGroup>)
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('true')
  })

  it('still collapses a finished group when the setting is off', () => {
    const { container, rerender } = render(<CollapsibleToolGroup count={2} isRunning autoExpand><div>row</div></CollapsibleToolGroup>)
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('true')
    rerender(<CollapsibleToolGroup count={2} isRunning={false} autoExpand={false}><div>row</div></CollapsibleToolGroup>)
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps a group the user closed by hand closed when its row remounts', () => {
    setStored({ toolCallsStartExpanded: true })
    function GroupHost() {
      const [mounted, setMounted] = useState(true)
      return (
        <RowDisclosureProvider resetKey="s1">
          <button data-testid="recycle" onClick={() => setMounted(m => !m)}>recycle</button>
          {mounted && <CollapsibleToolGroup count={2} disclosureKey="ctg-1"><div>row</div></CollapsibleToolGroup>}
        </RowDisclosureProvider>
      )
    }
    const { container } = render(<GroupHost />)
    fireEvent.click(groupHeader(container))
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByTestId('recycle'))
    fireEvent.click(screen.getByTestId('recycle'))
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps a group the user closed by hand closed', () => {
    setStored({ toolCallsStartExpanded: true })
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    fireEvent.click(groupHeader(container))
    expect(groupHeader(container).getAttribute('aria-expanded')).toBe('false')
  })
})
