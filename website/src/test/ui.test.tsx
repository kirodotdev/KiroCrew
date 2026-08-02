import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Card, CardTitle, Btn, SendBtn, Input, SearchInput, Badge, SourceBadge, StatCard, EmptyState, PageHeader, PanelSectionHeader } from '../components/ui'

describe('Card', () => {
  it('renders children', () => {
    render(<Card>Hello</Card>)
    expect(screen.getByText('Hello')).toBeInTheDocument()
  })
  it('applies custom className', () => {
    const { container } = render(<Card className="custom">X</Card>)
    expect(container.firstChild).toHaveClass('custom')
  })
})

describe('CardTitle', () => {
  it('renders as h3', () => {
    render(<CardTitle>Title</CardTitle>)
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Title')
  })
})

describe('Btn', () => {
  it('calls onClick', () => {
    const fn = vi.fn()
    render(<Btn onClick={fn}>Click</Btn>)
    fireEvent.click(screen.getByText('Click'))
    expect(fn).toHaveBeenCalledOnce()
  })
  it('can be disabled', () => {
    const fn = vi.fn()
    render(<Btn onClick={fn} disabled>Click</Btn>)
    expect(screen.getByText('Click')).toBeDisabled()
  })
  it('applies danger styling class', () => {
    render(<Btn onClick={() => {}} danger>Del</Btn>)
    expect(screen.getByText('Del').className).toContain('hover:text-danger')
  })
})

describe('SendBtn', () => {
  it('renders and fires onClick', () => {
    const fn = vi.fn()
    render(<SendBtn onClick={fn}>Send</SendBtn>)
    fireEvent.click(screen.getByText('Send'))
    expect(fn).toHaveBeenCalledOnce()
  })
})

describe('Input', () => {
  it('renders with placeholder', () => {
    render(<Input placeholder="Type here" />)
    expect(screen.getByPlaceholderText('Type here')).toBeInTheDocument()
  })
})

describe('SearchInput', () => {
  it('renders with search icon and placeholder', () => {
    render(<SearchInput placeholder="Search…" />)
    expect(screen.getByPlaceholderText('Search…')).toBeInTheDocument()
  })
})

describe('Badge', () => {
  it.each(['ok', 'err', 'warn', 'aim'] as const)('renders %s variant', (variant) => {
    render(<Badge variant={variant}>Label</Badge>)
    expect(screen.getByText('Label')).toBeInTheDocument()
  })
})

describe('SourceBadge', () => {
  it('renders source text', () => {
    render(<SourceBadge source="package" />)
    expect(screen.getByText('package')).toBeInTheDocument()
  })
})

describe('StatCard', () => {
  it('renders label and value', () => {
    render(<StatCard label="Uptime" value="5h" />)
    expect(screen.getByText('Uptime')).toBeInTheDocument()
    expect(screen.getByText('5h')).toBeInTheDocument()
  })
  it('shows skeleton when value is null', () => {
    const { container } = render(<StatCard label="X" value={null} />)
    expect(container.querySelector('.skeleton')).toBeInTheDocument()
  })
})

describe('EmptyState', () => {
  it('renders icon, title, and subtitle', () => {
    render(<EmptyState icon="list" title="Nothing here" subtitle="Add something" />)
    expect(screen.getByText('Nothing here')).toBeInTheDocument()
    expect(screen.getByText('Add something')).toBeInTheDocument()
  })
})

describe('PageHeader', () => {
  it('renders title and subtitle', () => {
    render(<PageHeader title="Dashboard" subtitle="Overview" />)
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })
})

// ── PanelSectionHeader ──────────────────────────────────────────────────────
//
// This primitive exists to stop side-panel section headers from drifting apart
// again, so its tests pin the two properties that drift would break rather than
// just "it renders the label". Both are asserted as NEGATIVES, because the
// regression is always something being ADDED back:
//
//  - no `uppercase`: text-transform is a no-op on CJK, so an uppercased
//    micro-header carries hierarchy in English and none at all in zh-CN.
//  - no opacity modifier on the text: dimming the label to text-muted/40
//    (~1.7:1 on both default themes) and the count to text-muted/50 (~1.9:1)
//    both fall under WCAG 1.4.3's 4.5:1 for text this size. Hierarchy has to
//    come from weight and size instead.
describe('PanelSectionHeader', () => {
  it('renders the label and the count as separate nodes', () => {
    render(<PanelSectionHeader label="Changed files" count={3} />)
    // Not `getByText('Changed files 3')` — the count must NOT be interpolated
    // into the translated label, which is a concatenation seam.
    expect(screen.getByText('Changed files')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('omits the count node entirely when no count is given', () => {
    render(<PanelSectionHeader label="Resources" />)
    const header = screen.getByTestId('panel-section-header')
    // Label + hairline rule only.
    expect(header.children).toHaveLength(2)
  })

  it('renders a zero count rather than treating it as absent', () => {
    render(<PanelSectionHeader label="Resources" count={0} />)
    expect(screen.getByText('0')).toBeInTheDocument()
  })

  it('never uppercases the label, so CJK keeps its hierarchy', () => {
    render(<PanelSectionHeader label="本次会话" count={2} />)
    const header = screen.getByTestId('panel-section-header')
    expect(header.innerHTML).not.toMatch(/\buppercase\b/)
    expect(screen.getByText('本次会话')).toBeInTheDocument()
  })

  it('never dims the label or count with an opacity modifier', () => {
    render(<PanelSectionHeader label="Changed files" count={3} />)
    const header = screen.getByTestId('panel-section-header')
    expect(screen.getByText('Changed files')).toHaveClass('text-muted')
    expect(screen.getByText('3')).toHaveClass('text-muted')
    // `text-muted/40`, `text-muted/50`, … on either node.
    expect(header.innerHTML).not.toMatch(/text-muted\/\d/)
  })

  it('places trailing content after the rule so it cannot shift the label', () => {
    render(<PanelSectionHeader label="Resources" count={1} trailing={<span>resolving…</span>} />)
    const header = screen.getByTestId('panel-section-header')
    const rule = header.querySelector('.h-px')
    const trailing = screen.getByText('resolving…')
    expect(rule).toBeInTheDocument()
    // DOCUMENT_POSITION_FOLLOWING === 4
    expect(rule!.compareDocumentPosition(trailing) & 4).toBeTruthy()
  })

  it('merges caller margins without dropping its own layout classes', () => {
    render(<PanelSectionHeader label="Resources" className="mt-3" />)
    const header = screen.getByTestId('panel-section-header')
    expect(header).toHaveClass('mt-3')
    expect(header).toHaveClass('flex')
  })
})
