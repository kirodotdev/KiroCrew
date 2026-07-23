import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Card, CardTitle, Btn, SendBtn, Input, SearchInput, Badge, SourceBadge, StatCard, EmptyState, PageHeader } from '../components/ui'

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
