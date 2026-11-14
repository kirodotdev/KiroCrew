/**
 * ErrorNotice `messagePlacement`.
 *
 * A notice whose `message` is a raw server string ("config store unavailable")
 * keeps it as the message because that string is the errorReport journal key
 * the hand-off reads. `messagePlacement="below"` lets the plain-language title
 * lead and puts the raw string under it in the inline variant. The block
 * variant always puts the message under the title (rfc-error-notice-impact.md,
 * layout 2), so it reads no placement.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ErrorNotice from '../components/ErrorNotice'
import {
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const RAW = 'config store unavailable (503)'
const navigated: string[] = []

beforeEach(() => {
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  navigated.length = 0
  sessionStorage.clear()
  installSoftNavigate(to => { navigated.push(to) })
})

afterEach(() => { __resetNavSeamForTests() })

describe('ErrorNotice messagePlacement', () => {
  it.each([undefined, 'beside', 'below'] as const)('block variant with messagePlacement=%s: the message always takes its own line under the title', (placement) => {
    render(<ErrorNotice title="Folder order could not be read" message={RAW} messagePlacement={placement} testId="n" />)
    const notice = screen.getByTestId('n')
    // The title still leads, and the text still reads as one run to assistive tech.
    expect(notice.textContent).toBe(`Folder order could not be read ${RAW}`)
    const raw = [...notice.querySelectorAll('span')].find(el => el.textContent === RAW)
    expect(raw!.className).toMatch(/\bblock\b/)
    expect(raw!.previousElementSibling?.textContent).toBe('Folder order could not be read ')
    expect(notice).toHaveAttribute('role', 'alert')
  })

  it('below: the inline variant wraps the row so the message takes the line under the title', () => {
    render(<ErrorNotice variant="inline" title="Folder order could not be read" message={RAW} messagePlacement="below" testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice.className).toContain('flex-wrap')
    const raw = [...notice.querySelectorAll('span')].find(el => el.textContent === RAW)
    expect(raw!.className).toContain('basis-full')
    expect(raw!.className).toContain('text-[11px]')
  })

  it('below without a title is the plain message: there is nothing to sit under', () => {
    render(<ErrorNotice message={RAW} messagePlacement="below" testId="n" />)
    expect(screen.getByTestId('n').textContent).toBe(RAW)
  })

  it('below keeps the raw string as the journal key: the hand-off still recovers the structured report from it', async () => {
    recordError({ source: 'api', message: RAW, status: 503, code: 'config_store_unavailable', endpoint: '/api/config/kirocrew' })
    render(<ErrorNotice title="Folder order could not be read" message={RAW} messagePlacement="below" askAgent actionPlacement="below" testId="n" />)
    const handoff = screen.getByRole('button', { name: /ask the agent/i })
    // Rendered in the action row, inside the text column, under the message.
    expect(screen.getByTestId('n').querySelector('[data-notice-actions]')?.contains(handoff)).toBe(true)
    await userEvent.click(handoff)
    expect(navigated).toEqual(['/chat'])
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain(RAW)
    expect(staged).toContain('config_store_unavailable')
    expect(staged).toContain('/api/config/kirocrew')
  })

  it('scrollMessage keeps one built-in hand-off outside the message and preserves its report and callbacks', () => {
    const onHandoff = vi.fn()
    const onDismiss = vi.fn()
    const report = recordError({
      source: 'api', message: RAW, status: 503,
      code: 'config_store_unavailable', endpoint: '/api/config/kirocrew',
    })
    render(
      <ErrorNotice
        message="The workspace could not be opened."
        report={report}
        scrollMessage
        askAgent
        actionPlacement="below"
        onHandoff={onHandoff}
        onDismiss={onDismiss}
      />,
    )
    const notice = screen.getByRole('alert')
    const text = within(notice).getByText('The workspace could not be opened.')
    const handoff = within(notice).getByRole('button', { name: /ask the agent/i })
    expect(text.contains(handoff)).toBe(false)
    expect(within(notice).getAllByRole('button', { name: /ask the agent/i })).toHaveLength(1)
    fireEvent.click(handoff)
    expect(navigated).toEqual(['/chat'])
    expect(onHandoff).toHaveBeenCalledOnce()
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain(RAW)
    expect(staged).toContain('config_store_unavailable')
    expect(staged).toContain('/api/config/kirocrew')
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it.each(['beside', 'below'] as const)('scrollMessage leaves inline notices unchanged with %s actions', actionPlacement => {
    const props = {
      message: RAW, title: 'Save failed', askAgent: true, actionPlacement,
      onDismiss: () => {}, footer: 'Try again later',
    }
    const { rerender } = render(<ErrorNotice {...props} variant="inline" />)
    const before = screen.getByRole('alert').outerHTML
    rerender(<ErrorNotice {...props} variant="inline" scrollMessage />)
    expect(screen.getByRole('alert').outerHTML).toBe(before)
  })

  it.each(['block', 'inline'] as const)('messageRef hands the %s variant\'s message span to the host', variant => {
    // The host that bounds and scrolls the message measures this node for its
    // edge cue, so the ref must land on the span that holds the text alone.
    const ref = { current: null as HTMLSpanElement | null }
    render(<ErrorNotice variant={variant} title="Save failed" message={RAW} messageRef={ref} testId="n" />)
    expect(ref.current).not.toBeNull()
    expect(ref.current!.tagName).toBe('SPAN')
    expect(ref.current!.textContent).toBe(RAW)
    expect(screen.getByTestId('n').contains(ref.current)).toBe(true)
  })
})
