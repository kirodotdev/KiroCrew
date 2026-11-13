/**
 * "Reasoning starts expanded" and a ChatPanel's `expandSteps`.
 *
 * The stored chat setting feeds the initial disclosure of a "Thought process"
 * row (ThinkingBlock). Default off, so a client with no stored choice keeps
 * today's collapsed rows. ExpandStepsContext, which ChatPanel provides from
 * `expandSteps`, opens reasoning and tool rows and shows working steps inline
 * for that transcript only, and never writes the saved chat config.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useState, type ReactNode } from 'react'
import ThinkingBlock from '../pages/chat/ThinkingBlock'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import TurnBlock from '../pages/chat/TurnBlock'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { RowDisclosureProvider } from '../pages/chat/rowDisclosure'
import { ExpandStepsContext } from '../hooks/useToolCallsStartExpanded'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

const CFG = 'mc-chat-config'
const setStored = (cfg: Record<string, unknown>) => localStorage.setItem(CFG, JSON.stringify(cfg))

beforeEach(() => { localStorage.clear() })

const isOpen = (c: HTMLElement) => c.querySelector('button[aria-expanded]')!.getAttribute('aria-expanded')
const Expanded = ({ children }: { children: ReactNode }) => <ExpandStepsContext.Provider value>{children}</ExpandStepsContext.Provider>

describe('chat config: reasoningStartsExpanded', () => {
  it('defaults to off', () => {
    expect(loadChatConfig().reasoningStartsExpanded).toBe(false)
  })

  it('coerces a stored non-boolean to off', () => {
    setStored({ reasoningStartsExpanded: 'yes' })
    expect(loadChatConfig().reasoningStartsExpanded).toBe(false)
  })
})

describe('ThinkingBlock honours "Reasoning starts expanded"', () => {
  it('starts collapsed when the setting is off', () => {
    const { container } = render(<ThinkingBlock content="settled reasoning" />)
    expect(isOpen(container)).toBe('false')
    expect(screen.queryByTestId('thinking-body')).toBeNull()
  })

  it('starts expanded when the setting is on', () => {
    setStored({ reasoningStartsExpanded: true })
    const { container } = render(<ThinkingBlock content="settled reasoning" />)
    expect(isOpen(container)).toBe('true')
    expect(screen.getByTestId('thinking-body').textContent).toContain('settled reasoning')
  })

  it('keeps a row the user closed by hand closed when it remounts', () => {
    setStored({ reasoningStartsExpanded: true })
    function Host() {
      const [mounted, setMounted] = useState(true)
      return (
        <RowDisclosureProvider resetKey="s1">
          <button data-testid="recycle" onClick={() => setMounted(m => !m)}>recycle</button>
          {mounted && <ThinkingBlock content="settled reasoning" disclosureKey="think-1" />}
        </RowDisclosureProvider>
      )
    }
    render(<Host />)
    fireEvent.click(screen.getByRole('button', { expanded: true }))
    expect(screen.getByRole('button', { expanded: false })).toBeTruthy()
    fireEvent.click(screen.getByTestId('recycle'))
    fireEvent.click(screen.getByTestId('recycle'))
    expect(screen.getByRole('button', { expanded: false })).toBeTruthy()
  })

  it('opens a reasoning-only group, and leaves a tool group collapsed', () => {
    setStored({ reasoningStartsExpanded: true })
    render(
      <>
        <div data-testid="reasoning"><CollapsibleToolGroup count={2} reasoningOnly><div>step</div></CollapsibleToolGroup></div>
        <div data-testid="tools"><CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup></div>
      </>,
    )
    expect(isOpen(screen.getByTestId('reasoning'))).toBe('true')
    expect(isOpen(screen.getByTestId('tools'))).toBe('false')
  })
})

describe('ExpandStepsContext (ChatPanel expandSteps)', () => {
  it('opens reasoning and tool rows with both settings off, and writes no config', () => {
    const { container } = render(
      <Expanded>
        <div data-testid="thinking"><ThinkingBlock content="settled reasoning" /></div>
        <div data-testid="tools"><CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup></div>
      </Expanded>,
    )
    expect(isOpen(screen.getByTestId('thinking'))).toBe('true')
    expect(isOpen(screen.getByTestId('tools'))).toBe('true')
    expect(container.textContent).toContain('settled reasoning')
    expect(localStorage.getItem(CFG)).toBeNull()
  })

  it('shows working steps inline when "Show Thinking Inline" is off', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'Checking the config first.', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read_file', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'The config sets the timeout to thirty seconds, so the retry fires too early.', ts: '3' }, idx: 2 },
    ]
    const turn: Extract<DisplayItem, { kind: 'turn' }> = { kind: 'turn', items, complete: true }
    const renderItem = (it: TurnItem, i: number) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    const step = (c: HTMLElement) => c.querySelector('[data-testid="item-0"]')
    const folded = (el: Element | null) => el === null || el.closest('[data-collapsed="true"]') !== null

    const plain = render(<TurnBlock turn={turn} renderItem={renderItem} collapseAll />)
    expect(folded(step(plain.container))).toBe(true)
    plain.unmount()

    const expanded = render(<Expanded><TurnBlock turn={turn} renderItem={renderItem} collapseAll /></Expanded>)
    expect(folded(step(expanded.container))).toBe(false)
  })
})
