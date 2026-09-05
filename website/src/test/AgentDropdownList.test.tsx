import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter } from '../components/AgentDropdownList'
import type { AgentItem } from '../components/AgentDropdownList'

// jsdom doesn't implement scrollIntoView
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

const agents: AgentItem[] = [
  { name: 'kirocrew', source: 'kirocrew', description: 'Main agent' },
  { name: 'builtin', source: 'builtin' },
]

describe('AgentDropdownList', () => {
  it('renders all agents', () => {
    render(<AgentDropdownList agents={agents} activeAgent="kirocrew" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getAllByText('kirocrew').length).toBeGreaterThan(0)
    expect(screen.getAllByText('builtin').length).toBeGreaterThan(0)
  })

  it('shows "No matches" when agents list is empty', () => {
    render(<AgentDropdownList agents={[]} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('No matches')).toBeInTheDocument()
  })

  it('calls onSelect with the agent name when clicked', () => {
    const onSelect = vi.fn()
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={onSelect} />)
    const btn = Array.from(document.querySelectorAll('button')).find(
      b => b.querySelector('.font-mono')?.textContent === 'kirocrew'
    )
    fireEvent.click(btn!)
    expect(onSelect).toHaveBeenCalledWith('kirocrew')
  })

  it('shows description when present', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('Main agent')).toBeInTheDocument()
  })

  it('declares no scroll container of its own, leaving the host as the single scroll owner', () => {
    // Both hosts (ChatPage and ChatPane) wrap this list in their own
    // `overflow-y-auto max-h-[280px]` listbox. When the component also carried
    // `overflow-y-auto max-h-[300px]`, the panel showed two nested scrollbars
    // (#6375). Every option row's ancestor inside the component must stay
    // overflow-free so exactly one scrollbar — the host's — appears. Checked
    // via class AND inline style, so neither a Tailwind overflow utility nor
    // a `style={{ overflowY: 'auto' }}` can reintroduce the nested scroller.
    const { container } = render(
      <AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />
    )
    const option = screen.getAllByRole('option')[0]
    let checked = 0
    for (let el: HTMLElement | null = option.parentElement; el && container.contains(el); el = el.parentElement) {
      checked++
      expect(el.className).not.toMatch(/overflow-(y-)?(auto|scroll)/)
      expect(el.style.overflowY).toMatch(/^(|visible)$/)
      expect(el.style.overflow).toMatch(/^(|visible)$/)
    }
    // Guard against the loop passing vacuously (e.g. rows moved into a portal).
    expect(checked).toBeGreaterThan(0)
  })
})

describe('AgentDropdownList hosts own the scroll (#6375)', () => {
  // The component deliberately declares no scroll container (see the test
  // above), which moves the "exactly one scroll owner" invariant into the two
  // hosts. Pin it structurally: each render site must wrap the list in a
  // listbox that carries the overflow + max-height, or the pop-up grows
  // unbounded with no failing test.
  // vitest's cwd is website/ (the vitest config root), and import.meta.url is
  // not file-scheme under its transform, so resolve from cwd instead.
  const hosts = [
    ['ChatPage', join(process.cwd(), 'src', 'pages', 'chat', 'ChatPageView.tsx')],
    ['ChatPane', join(process.cwd(), 'src', 'components', 'ChatPane.tsx')],
  ] as const

  it.each(hosts)('%s wraps the list in a scroll-owning listbox', (_name, file) => {
    const src = readFileSync(file, 'utf8')
    const sites = [...src.matchAll(/<AgentDropdownList[\s>]/g)]
    expect(sites.length).toBeGreaterThan(0)
    for (const site of sites) {
      // The wrapper opens within the few hundred chars above the render site.
      const windowBefore = src.slice(Math.max(0, site.index! - 600), site.index!)
      expect(windowBefore).toMatch(/role="listbox"[^>]*className="[^"]*overflow-y-auto[^"]*max-h-\[/)
    }
  })
})

describe('AgentDropdownList default-agent affordance', () => {
  it('labels the default agent with a Default pill instead of its source badge', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getByText('Default')).toBeInTheDocument()
  })

  it('puts no second control inside the option rows', () => {
    // A row's one job is picking the agent for this session. A nested control had to
    // stopPropagation to keep the two apart, and its scope ("for new sessions") could
    // only live in a tooltip — the footer row states it on screen instead.
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={() => {}} />)
    for (const option of screen.getAllByRole('option')) {
      expect(option.querySelector('[role="button"]')).toBeNull()
    }
  })

  it('explains the two same-row markers rather than relying on colour alone', () => {
    render(<AgentDropdownList agents={agents} activeAgent="kirocrew" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getByTitle('New sessions start with this agent')).toBeInTheDocument()
    expect(screen.getByTitle('Active in this session')).toBeInTheDocument()
  })
})

describe('DefaultAgentRow', () => {
  it('names both the agent it writes and the scope it writes it to', () => {
    // An unqualified "Set as default" reads as session-scoped in a pop-up whose other
    // job is switching the agent for this session, and a bare icon can only put the
    // scope in a tooltip.
    render(<DefaultAgentRow agentName="reviewer" isDefault={false} onSetDefault={() => {}} />)
    expect(screen.getByRole('button', { name: 'Set reviewer as default agent for new sessions' })).toBeInTheDocument()
  })

  it('writes the default when activated', () => {
    const onSetDefault = vi.fn()
    render(<DefaultAgentRow agentName="reviewer" isDefault={false} onSetDefault={onSetDefault} />)
    fireEvent.click(screen.getByRole('button'))
    expect(onSetDefault).toHaveBeenCalledTimes(1)
  })

  it('reports the state instead of offering a no-op write once the agent holds it', () => {
    // Clearing the default is destructive (the product ends up with none) and must not
    // hide behind the same gesture that sets one. Only the Templates page clears it.
    const onSetDefault = vi.fn()
    render(<DefaultAgentRow agentName="reviewer" isDefault onSetDefault={onSetDefault} />)
    const row = screen.getByRole('button', { name: 'Default agent for new sessions' })
    expect(row).toBeDisabled()
    expect(row).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(row)
    expect(onSetDefault).not.toHaveBeenCalled()
  })

  it('joins the listbox roving-focus ring, the only keyboard path to it', () => {
    // `useListboxKeyboard` consumes Tab to close the pop-up, so a plain button in
    // the footer is pointer-only however correct its markup is. The hook moves real
    // focus across `[data-option],[role="option"]`, and its own wiring notes say an
    // action row must carry `data-option` + tabIndex={-1} to be reachable.
    render(<DefaultAgentRow agentName="reviewer" isDefault={false} onSetDefault={() => {}} />)
    const row = screen.getByRole('button')
    expect(row).toHaveAttribute('data-option')
    expect(row).toHaveAttribute('tabindex', '-1')
  })

  it('leaves the ring once it is disabled, so focus never stops on a dead row', () => {
    render(<DefaultAgentRow agentName="reviewer" isDefault onSetDefault={() => {}} />)
    expect(screen.getByRole('button')).not.toHaveAttribute('data-option')
  })
})

describe('ManageAgentsFooter', () => {
  it('calls onManage when the link is activated', () => {
    const onManage = vi.fn()
    render(<ManageAgentsFooter onManage={onManage} />)
    fireEvent.click(screen.getByText('Manage agents…'))
    expect(onManage).toHaveBeenCalledTimes(1)
  })

  it('stays silent when the default-agent write succeeded', () => {
    render(<ManageAgentsFooter onManage={() => {}} />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('surfaces a failed default-agent write instead of swallowing it', () => {
    // The write is fire-and-forget, so without this a rejected request looks exactly
    // like a successful one.
    render(<ManageAgentsFooter onManage={() => {}} error />)
    expect(screen.getByRole('alert')).toHaveTextContent('Could not change the default agent')
  })
})
