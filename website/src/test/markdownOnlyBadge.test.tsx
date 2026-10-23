/**
 * A markdown-only agent is badged in the agent pickers when the harness
 * the chat runs cannot load it, and stays selectable.
 *
 * The catalog sends `markdown_only` per row plus `session_backend` and
 * `markdown_refused_backends` / `markdown_spec_backends`; `useAgents` stamps the badge facts once, and both
 * row renderers (the chat pop-up's `AgentDropdownList` and the form pickers'
 * `AgentSelector`) draw the same pill from them.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { render, screen, fireEvent, renderHook as rtlRenderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import AgentDropdownList, { type AgentItem } from '../components/AgentDropdownList'
import AgentSelector, { type KiroCrewAgent } from '../components/AgentSelector'
import { markdownBlock, withMarkdownBlocks } from '../lib/markdownAgents'
import { useAgents } from '../hooks/useAgents'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(() => Promise.resolve({})),
  },
}))

const { api } = await import('../api/client')
const mockApi = api as unknown as { agentCatalog: ReturnType<typeof vi.fn> }

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})
beforeEach(() => vi.clearAllMocks())

const onKiro = { session_backend: 'kiro', markdown_refused_backends: ['kiro'], markdown_spec_backends: ['kas'] }

describe('markdownBlock', () => {
  it('badges a markdown-only row on a harness the gateway says refuses it', () => {
    expect(markdownBlock({ markdown_only: true }, onKiro)).toEqual({ backend: 'kiro', capable: ['kas'] })
  })

  it('says nothing for a JSON row or a capable harness', () => {
    expect(markdownBlock({ markdown_only: false }, onKiro)).toBeUndefined()
    expect(markdownBlock({}, onKiro)).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, { ...onKiro, session_backend: 'kas' })).toBeUndefined()
  })

  it('says nothing on a harness that starts the session anyway', () => {
    // codex, claude and the others never verify agent activation, so they are
    // outside the refused set: "won't start" would be false there.
    expect(markdownBlock({ markdown_only: true }, { ...onKiro, session_backend: 'codex' })).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, { ...onKiro, markdown_refused_backends: [] })).toBeUndefined()
  })

  it('stays quiet on an older gateway or malformed facts rather than guess', () => {
    expect(markdownBlock({ markdown_only: true }, undefined)).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, {})).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, { session_backend: 'kiro' })).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, { session_backend: 'kiro', markdown_spec_backends: ['kas'] })).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, { ...onKiro, markdown_spec_backends: [] })).toBeUndefined()
    expect(markdownBlock({ markdown_only: true }, { ...onKiro, session_backend: '' })).toBeUndefined()
    expect(markdownBlock({ markdown_only: 'yes' }, onKiro)).toBeUndefined()
  })

  it('reads the capable set from the gateway instead of naming one', () => {
    const facts = { ...onKiro, markdown_spec_backends: ['kas', 'future'] }
    expect(markdownBlock({ markdown_only: true }, facts)?.capable).toEqual(['kas', 'future'])
    expect(markdownBlock({ markdown_only: true }, { ...facts, session_backend: 'future', markdown_refused_backends: ['kiro', 'future'] })).toBeUndefined()
  })

  it('stamps only the rows that need it', () => {
    const rows = withMarkdownBlocks([{ name: 'md', markdown_only: true }, { name: 'js', markdown_only: false }], onKiro)
    expect(rows[0].markdown_block).toEqual({ backend: 'kiro', capable: ['kas'] })
    expect('markdown_block' in rows[1]).toBe(false)
  })
})

describe('the chat agent pop-up', () => {
  const agents: AgentItem[] = [
    { name: 'kirocrew', source: 'kirocrew' },
    { name: 'md-probe', source: 'builtin', description: 'Markdown probe', markdown_block: { backend: 'kiro', capable: ['kas'] } },
  ]

  it('shows a compact "Kiro Agent only" pill with the reason on hover', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    const badges = screen.getAllByTestId('markdown-only-badge')
    expect(badges).toHaveLength(1)
    expect(badges[0].firstChild?.textContent).toBe('Kiro Agent only')
    // The reason also rides in the option's accessible text, hidden on screen.
    const hidden = badges[0].querySelector('.sr-only')
    expect(hidden?.textContent?.trim()).toBe(badges[0].getAttribute('title'))
    expect(badges[0].closest('[role="option"]')?.textContent).toContain("This agent won't start here.")
    expect(badges[0].getAttribute('title')).toBe(
      "This agent won't start here. It's defined in markdown, and Kiro CLI runs JSON agents only. Use Kiro Agent, or add a JSON file for it.",
    )
  })

  it('keeps the badged row selectable', () => {
    const onSelect = vi.fn()
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={onSelect} />)
    const option = screen.getByTestId('markdown-only-badge').closest('[role="option"]') as HTMLElement
    expect(option).not.toHaveAttribute('aria-disabled')
    fireEvent.click(option)
    expect(onSelect).toHaveBeenCalledWith('md-probe')
  })
})

describe('the form agent picker (Schedule, Channels, Projects, Webhooks)', () => {
  it('draws the same pill and still selects the row', () => {
    const agents: KiroCrewAgent[] = [
      { name: 'coding', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: '' },
      {
        name: 'md-probe', kiro_agent: 'md-probe', workspace: 'default', memory_store: 'default', description: '', source: '',
        markdown_only: true, markdown_block: { backend: 'kiro', capable: ['kas'] },
      },
    ]
    const onChange = vi.fn()
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const badges = screen.getAllByTestId('markdown-only-badge')
    expect(badges).toHaveLength(1)
    fireEvent.click(badges[0].closest('[role="option"]') as HTMLElement)
    expect(onChange).toHaveBeenCalledWith('md-probe')
  })
})

describe('useAgents', () => {
  const renderHook = ((cb: never, opts?: Record<string, unknown>) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children)
    return rtlRenderHook(cb, { ...opts, wrapper })
  }) as typeof rtlRenderHook

  it('stamps the badge from the catalog the chat asked for', async () => {
    mockApi.agentCatalog.mockResolvedValue({
      agents: [
        { name: 'md-probe', selection_kind: 'template', kiro_agent: 'md-probe', markdown_only: true },
        { name: 'plain', selection_kind: 'template', kiro_agent: 'plain', markdown_only: false },
      ],
      default_agent: 'plain',
      ...onKiro,
    })
    const { result } = renderHook(() => useAgents(0, 'chat-1'))
    await waitFor(() => expect(result.current.choices).toHaveLength(2))
    const byName = Object.fromEntries(result.current.choices.map(c => [c.name, c.markdown_block]))
    expect(byName['md-probe']).toEqual({ backend: 'kiro', capable: ['kas'] })
    expect(byName.plain).toBeUndefined()
  })

  it('shows no badge when this chat runs a harness that loads markdown', async () => {
    mockApi.agentCatalog.mockResolvedValue({
      agents: [{ name: 'md-probe', selection_kind: 'template', kiro_agent: 'md-probe', markdown_only: true }],
      default_agent: 'md-probe',
      session_backend: 'kas',
      markdown_refused_backends: ['kiro'],
      markdown_spec_backends: ['kas'],
    })
    const { result } = renderHook(() => useAgents(0, 'dashboard:member-reviewer'))
    await waitFor(() => expect(result.current.choices).toHaveLength(1))
    expect(result.current.choices[0].markdown_block).toBeUndefined()
  })
})
