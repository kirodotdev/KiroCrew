import { render, screen, fireEvent } from '@testing-library/react'
import SearchResultsList from '../components/SearchResultsList'
import type { SearchMatch } from '../hooks/useMessageSearch'
import type { ChatMessage } from '../types'

const msg = (role: string, content: string): ChatMessage => ({ role, content, cls: '' })

const messages: ChatMessage[] = [
  msg('user', 'tell me about replication and how replication works'), // 2x "replication"
  msg('assistant', 'Replication copies data across nodes.'),          // 1x (capital R)
]

// Matches as useMessageSearch would produce them (case-insensitive default):
// msg0 occ0, msg0 occ1, msg1 occ0
const matches: SearchMatch[] = [
  { msgIdx: 0, occ: 0 },
  { msgIdx: 0, occ: 1 },
  { msgIdx: 1, occ: 0 },
]

const base = {
  matches,
  currentIdx: 0,
  messages,
  term: 'replication',
  caseSensitive: false,
  onJump: vi.fn(),
}

describe('SearchResultsList', () => {
  it('renders nothing when term is empty', () => {
    const { container } = render(<SearchResultsList {...base} term="" />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one row per match occurrence', () => {
    render(<SearchResultsList {...base} onJump={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(3)
  })

  it('highlights the matched term in each snippet via <mark>', () => {
    const { container } = render(<SearchResultsList {...base} onJump={vi.fn()} />)
    const marks = container.querySelectorAll('mark')
    expect(marks.length).toBe(3)
    // Case-insensitive search preserves the original casing in the snippet.
    expect(marks[2].textContent).toBe('Replication')
  })

  it('builds a context snippet around the occurrence', () => {
    render(<SearchResultsList {...base} onJump={vi.fn()} />)
    const rows = screen.getAllByRole('option')
    // Second occurrence in msg0 should show preceding context ("...how ").
    expect(rows[1].textContent).toMatch(/how/)
  })

  it('marks the current row as selected', () => {
    render(<SearchResultsList {...base} currentIdx={1} onJump={vi.fn()} />)
    const rows = screen.getAllByRole('option')
    expect(rows[1].getAttribute('aria-selected')).toBe('true')
    expect(rows[0].getAttribute('aria-selected')).toBe('false')
  })

  it('labels rows by role', () => {
    render(<SearchResultsList {...base} onJump={vi.fn()} />)
    expect(screen.getAllByText('You').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Assistant').length).toBeGreaterThan(0)
  })

  it('jumps to the clicked occurrence', () => {
    const onJump = vi.fn()
    render(<SearchResultsList {...base} onJump={onJump} />)
    fireEvent.click(screen.getAllByRole('option')[2])
    expect(onJump).toHaveBeenCalledWith(2)
  })

  it('caps rendered rows at 200 and shows an overflow footer', () => {
    const many: SearchMatch[] = Array.from({ length: 250 }, (_, k) => ({ msgIdx: 0, occ: k }))
    render(<SearchResultsList {...base} matches={many} onJump={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(200)
    expect(screen.getByText(/Showing 200 of 250 matches/)).toBeTruthy()
  })

  it('windows so the active row stays rendered when currentIdx is past MAX_ROWS', () => {
    const many: SearchMatch[] = Array.from({ length: 250 }, (_, k) => ({ msgIdx: 0, occ: k }))
    render(<SearchResultsList {...base} matches={many} currentIdx={220} onJump={vi.fn()} />)
    const rows = screen.getAllByRole('option')
    expect(rows).toHaveLength(200)
    // The active match (idx 220) is windowed in and is the single selected row.
    const selected = rows.filter(r => r.getAttribute('aria-selected') === 'true')
    expect(selected).toHaveLength(1)
  })

  it('honors caseSensitive: only matches the exact case', () => {
    const msgs = [msg('user', 'replication then Replication')]
    const m: SearchMatch[] = [{ msgIdx: 0, occ: 0 }]
    const { container } = render(
      <SearchResultsList matches={m} currentIdx={0} messages={msgs} term="replication" caseSensitive onJump={vi.fn()} />,
    )
    // occ 0 of lowercase "replication" is at index 0; the capital one is ignored.
    expect(container.querySelector('mark')!.textContent).toBe('replication')
  })

  it('falls back gracefully when the occurrence is not found', () => {
    const msgs = [msg('user', 'foo bar')]
    const m: SearchMatch[] = [{ msgIdx: 0, occ: 5 }] // only 1 occurrence exists
    const { container } = render(
      <SearchResultsList matches={m} currentIdx={0} messages={msgs} term="foo" caseSensitive={false} onJump={vi.fn()} />,
    )
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(container.querySelector('mark')!.textContent).toBe('foo')
  })

  it('adds ellipsis at truncated boundaries and collapses whitespace', () => {
    const long = 'A'.repeat(80) + ' needle\nsecond line ' + 'B'.repeat(80)
    const msgs = [msg('assistant', long)]
    const m: SearchMatch[] = [{ msgIdx: 0, occ: 0 }]
    render(
      <SearchResultsList matches={m} currentIdx={0} messages={msgs} term="needle" caseSensitive={false} onJump={vi.fn()} />,
    )
    const text = screen.getByRole('option').textContent || ''
    expect(text).toContain('…')          // truncated on both sides
    expect(text).not.toContain('\n')     // newlines collapsed to spaces
  })

  it('does not crash when a match points at a missing message', () => {
    const m: SearchMatch[] = [{ msgIdx: 99, occ: 0 }] // out of range
    render(
      <SearchResultsList matches={m} currentIdx={0} messages={messages} term="replication" caseSensitive={false} onJump={vi.fn()} />,
    )
    expect(screen.getAllByRole('option')).toHaveLength(1)
  })
})
