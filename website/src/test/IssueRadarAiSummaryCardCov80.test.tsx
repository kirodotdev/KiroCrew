import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// MarkdownRenderer pulls the whole markdown pipeline in; the card's contract is
// WHICH text it hands over, so a passthrough keeps the assertion on that.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
const motion = { reduce: false }
vi.mock('framer-motion', () => ({ useReducedMotion: () => motion.reduce }))

const AiSummaryCard = (await import('../apps/issue-radar/components/AiSummaryCard')).default

const onRegenerate = vi.fn()

function renderCard(over: Partial<React.ComponentProps<typeof AiSummaryCard>> = {}) {
  return render(
    <AiSummaryCard
      summary=""
      fromCache
      loading={false}
      fetching={false}
      error={null}
      onRegenerate={onRegenerate}
      {...over}
    />,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  motion.reduce = false
})

afterEach(() => {
  vi.useRealTimers()
})

describe('AiSummaryCard — body states', () => {
  it('shims while generating, hiding the stale text', () => {
    renderCard({ summary: 'zzq-old-summary', loading: true })
    expect(screen.getByRole('status', { name: 'Generating AI summary' })).toBeInTheDocument()
    expect(screen.queryByTestId('md')).toBeNull()
  })

  it('treats a regenerate exactly like a first generation', () => {
    // Leaving the previous summary on screen during a refresh would make a stale
    // answer look current.
    renderCard({ summary: 'zzq-old-summary', fetching: true })
    expect(screen.getByRole('status', { name: 'Generating AI summary' })).toBeInTheDocument()
  })

  it('keeps a previous summary on a failed regenerate, and says so', async () => {
    renderCard({ summary: 'zzq-kept-summary', error: new Error('boom') })
    expect(screen.getByText(/Couldn't regenerate/)).toBeInTheDocument()
    expect(screen.getByTestId('md')).toHaveTextContent('zzq-kept-summary')
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })

  it('reports a failure with nothing to fall back on', async () => {
    renderCard({ error: new Error('boom') })
    expect(screen.getByText(/Couldn't generate a summary/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })

  it('names the subject in the empty state', () => {
    renderCard({ subject: 'zzq-noun' })
    expect(screen.getByText(/zzq-noun/)).toBeInTheDocument()
  })

  it('renders a cached summary whole, with no reveal', () => {
    renderCard({ summary: 'zzq-cached-body', fromCache: true })
    expect(screen.getByTestId('md')).toHaveTextContent('zzq-cached-body')
  })
})

describe('AiSummaryCard — typewriter reveal', () => {
  it('reveals a freshly generated summary progressively', async () => {
    vi.useFakeTimers()
    const text = 'x'.repeat(400)
    renderCard({ summary: text, fromCache: false })

    // First paint of a fresh summary starts empty, then grows on the interval.
    expect(screen.getByTestId('md').textContent).toBe('')
    await act(async () => { vi.advanceTimersByTime(22) })
    const partial = screen.getByTestId('md').textContent as string
    expect(partial.length).toBeGreaterThan(0)
    expect(partial.length).toBeLessThan(text.length)

    await act(async () => { vi.advanceTimersByTime(22 * 40) })
    expect(screen.getByTestId('md').textContent).toBe(text)
  })

  it('skips the reveal under reduced motion', () => {
    motion.reduce = true
    renderCard({ summary: 'zzq-fresh-body', fromCache: false })
    expect(screen.getByTestId('md')).toHaveTextContent('zzq-fresh-body')
  })
})

describe('AiSummaryCard — age label', () => {
  it('shows the age of a settled summary and re-reads it on a timer', async () => {
    vi.useFakeTimers()
    const generatedAt = new Date(Date.now() - 5 * 60_000).toISOString()
    renderCard({ summary: 'zzq-body', generatedAt })

    const header = screen.getByText('AI summary').parentElement as HTMLElement
    expect(header.textContent).toMatch(/5m/)
    // The tick exists so the label ages while the pane sits open.
    await act(async () => { vi.advanceTimersByTime(30_000) })
    expect(header.textContent).toMatch(/m/)
  })

  it('warns when activity is newer than the summary', () => {
    const generatedAt = new Date(Date.now() - 60 * 60_000).toISOString()
    const staleSince = new Date(Date.now() - 30 * 60_000).toISOString()
    renderCard({ summary: 'zzq-body', generatedAt, staleSince })
    const label = screen.getByText(/Outdated|outdated/i)
    expect(label).toBeInTheDocument()
  })

  it('shows no age while a generation is in flight', () => {
    renderCard({ summary: 'zzq-body', generatedAt: new Date().toISOString(), fetching: true })
    const header = screen.getByText('AI summary').parentElement as HTMLElement
    expect(header.textContent).not.toMatch(/ago|\dm/)
  })

  it('disables the refresh control while a fetch is in flight', async () => {
    renderCard({ summary: 'zzq-body', fetching: true })
    const refresh = screen.getByRole('button', { name: 'Regenerate AI summary' })
    expect(refresh).toHaveProperty('disabled', true)
    await userEvent.click(refresh)
    expect(onRegenerate).not.toHaveBeenCalled()
  })

  it('regenerates from the header control', async () => {
    renderCard({ summary: 'zzq-body' })
    await userEvent.click(screen.getByRole('button', { name: 'Regenerate AI summary' }))
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })
})
