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

describe('AgentDropdownList namespaces (member vs template)', () => {
  const both: AgentItem[] = [
    { name: 'reviewer', source: 'kirocrew', selection_kind: 'member', description: 'My reviewer' },
    { name: 'reviewer', source: 'builtin', selection_kind: 'template', description: 'Shared template' },
    { name: 'test-writer', source: 'package', selection_kind: 'template' },
  ]

  it('groups by kind and keeps a same-name member and template as two rows', () => {
    render(<AgentDropdownList agents={both} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    const groups = screen.getAllByRole('group')
    expect(groups.map(g => g.getAttribute('aria-label'))).toEqual(['Crewmates', 'Agent templates'])
    expect(screen.getAllByRole('option')).toHaveLength(3)
    expect(screen.getAllByText('reviewer')).toHaveLength(2)
  })

  it('drops the origin badge inside the grouped view and explains what a template pick is', () => {
    // The header already says what each row IS; a grey "package" / "kirocrew"
    // tag beside it only asks the reader to decode a second vocabulary. The
    // templates group instead carries the one fact a first-time picker needs:
    // a template runs on shared memory and enrols nothing.
    render(<AgentDropdownList agents={both} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.queryByText('package')).toBeNull()
    expect(screen.queryByText('kirocrew')).toBeNull()
    expect(screen.getByText(/runs the shared template on shared memory/i)).toBeInTheDocument()
  })

  it('drops the header and the templates hint when the list holds one kind only', () => {
    // With crewmates withheld (HIDE_CREWMATE_CHOICES) the list is templates
    // only. A header then separates nothing and the hint contrasts a template
    // against a crewmate the list never shows, so both go; the accessible
    // group label stays, and the rows keep their grouped rendering (no origin
    // badge) so the flag flips nothing but the chrome.
    const templatesOnly = both.filter(a => a.selection_kind === 'template')
    render(<AgentDropdownList agents={templatesOnly} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByRole('group', { name: 'Agent templates' })).toBeInTheDocument()
    expect(screen.queryByText('Agent templates')).toBeNull()
    expect(screen.queryByText(/runs the shared template on shared memory/i)).toBeNull()
    expect(screen.queryByText('package')).toBeNull()
    expect(screen.getAllByRole('option')).toHaveLength(2)
  })

  it('keeps the origin badge on the flat, name-only list', () => {
    render(<AgentDropdownList agents={[{ name: 'x', source: 'package' }]} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('package')).toBeInTheDocument()
  })

  it('reports the kind with the name, so the caller can send the namespace', () => {
    const onSelect = vi.fn()
    render(<AgentDropdownList agents={both} activeAgent="" defaultAgent="" onSelect={onSelect} />)
    const options = screen.getAllByRole('option')
    fireEvent.click(options[0])
    fireEvent.click(options[1])
    expect(onSelect).toHaveBeenNthCalledWith(1, 'reviewer', 'member')
    expect(onSelect).toHaveBeenNthCalledWith(2, 'reviewer', 'template')
  })

  it('draws the roster avatar on a crewmate row and none on a template row', () => {
    // A member is a crewmate with a face; a template is a definition. The same
    // CrewAvatar the roster renders (a decorative img) sits before the name on
    // member rows only, so a same-name pair is told apart at a glance.
    render(<AgentDropdownList agents={both} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    const options = screen.getAllByRole('option')
    expect(options[0].querySelector('img')).not.toBeNull()
    expect(options[1].querySelector('img')).toBeNull()
  })

  it('lights up only the row in the slot\'s recorded namespace', () => {
    render(<AgentDropdownList agents={both} activeAgent="reviewer" activeKind="template" defaultAgent="" onSelect={() => {}} />)
    const selected = screen.getAllByRole('option').map(o => o.getAttribute('aria-selected'))
    expect(selected).toEqual(['false', 'true', 'false'])
  })

  it('lights the member row when the slot recorded no namespace and a member holds the name', () => {
    // A slot restored from history (or from an older gateway) carries no kind.
    // The backend resolves a bare name member-first, so the member row is
    // what actually runs; the same-name template must not read as current.
    render(<AgentDropdownList agents={both} activeAgent="reviewer" defaultAgent="" onSelect={() => {}} />)
    const selected = screen.getAllByRole('option').map(o => o.getAttribute('aria-selected'))
    expect(selected).toEqual(['true', 'false', 'false'])
  })

  it('lights the template row when the slot recorded no namespace and no member holds the name', () => {
    const templateOnly: AgentItem[] = [
      { name: 'reviewer', source: 'kirocrew', selection_kind: 'member' },
      { name: 'planner', source: 'builtin', selection_kind: 'template' },
    ]
    render(<AgentDropdownList agents={templateOnly} activeAgent="planner" defaultAgent="" onSelect={() => {}} />)
    const selected = screen.getAllByRole('option').map(o => o.getAttribute('aria-selected'))
    expect(selected).toEqual(['false', 'true'])
  })

  it('renders a flat list, with no group headers, for a name-only roster', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.queryByRole('group')).not.toBeInTheDocument()
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
    ['ChatPage', join(process.cwd(), 'src', 'pages', 'ChatPage.tsx')],
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
