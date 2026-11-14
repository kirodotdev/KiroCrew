/**
 * ErrorNotice `impact`, `nested` and `actions`
 * (docs/request-for-change/rfc-error-notice-impact.md).
 *
 * The look follows what the failure costs: `blocking` (the default) keeps the
 * red box and `role="alert"`; `partial` is amber and polite; `minor` is a grey
 * line and polite. `nested` drops the notice's own box inside a host that
 * already frames it. `actions` puts the host's own buttons in the action row,
 * before "Ask the agent", which stays a link.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'
import { setGatewayConnected } from '../utils/errorReport'

beforeEach(() => { setGatewayConnected(true) })

const ICONS = { blocking: '.lucide-circle-alert', partial: '.lucide-triangle-alert', minor: '.lucide-info' } as const

describe('ErrorNotice impact', () => {
  it('defaults to blocking: red box, circle icon, role="alert"', () => {
    render(<ErrorNotice message="Load failed" testId="n" noHandoffReason="unit test" />)
    const n = screen.getByTestId('n')
    expect(n).toHaveAttribute('role', 'alert')
    expect(n.className).toContain('bg-danger-subtle')
    expect(n.className).toContain('border-danger/40')
    expect(n.querySelector(ICONS.blocking)).not.toBeNull()
  })

  it.each([
    ['blocking', 'alert', 'bg-danger-subtle'],
    ['partial', 'status', 'bg-warn-subtle'],
  ] as const)('%s block: role=%s, fill %s, its own icon', (impact, role, fillClass) => {
    render(<ErrorNotice impact={impact} message="Balance unavailable" testId="n" noHandoffReason="unit test" />)
    const n = screen.getByTestId('n')
    expect(n).toHaveAttribute('role', role)
    expect(n.className).toContain(fillClass)
    expect(n.querySelector(ICONS[impact])).not.toBeNull()
  })

  it('minor draws no box and is polite', () => {
    render(<ErrorNotice impact="minor" message="Forecast unavailable" testId="n" noHandoffReason="unit test" />)
    const n = screen.getByTestId('n')
    expect(n).toHaveAttribute('role', 'status')
    expect(n.className).not.toMatch(/\bborder-(danger|warn)\/40\b/)
    expect(n.className).not.toMatch(/\bbg-(danger|warn)-subtle\b/)
    expect(n.querySelector(ICONS.minor)).not.toBeNull()
  })

  it('blocking body uses the softer body colour, title the ink colour', () => {
    render(<ErrorNotice title="Save failed" message="The folder is read-only." testId="n" noHandoffReason="unit test" />)
    expect(screen.getByText('Save failed').className).toContain('text-danger-ink')
    expect(screen.getByText('The folder is read-only.').className).toContain('text-danger-body')
  })

  it('puts the message on its own line under the title', () => {
    render(<ErrorNotice title="Save failed" message="The folder is read-only." testId="n" noHandoffReason="unit test" />)
    const title = screen.getByText('Save failed')
    const body = screen.getByText('The folder is read-only.')
    expect(title.className).toMatch(/\bblock\b/)
    expect(body.className).toMatch(/\bblock\b/)
    expect(title.nextElementSibling).toBe(body)
  })

  it.each(['partial', 'minor'] as const)('announce={false} drops the role at %s too', (impact) => {
    render(<ErrorNotice impact={impact} message="x" testId="n" announce={false} noHandoffReason="unit test" />)
    expect(screen.getByTestId('n')).not.toHaveAttribute('role')
  })

  it.each([
    ['blocking', 'alert', ICONS.blocking],
    ['partial', 'status', ICONS.partial],
    ['minor', 'status', ICONS.minor],
  ] as const)('inline %s: role=%s and its own icon', (impact, role, icon) => {
    render(<ErrorNotice variant="inline" impact={impact} message="x" testId="n" noHandoffReason="unit test" />)
    const n = screen.getByTestId('n')
    expect(n).toHaveAttribute('role', role)
    expect(n.querySelector(icon)).not.toBeNull()
  })
})

describe('ErrorNotice nested', () => {
  it.each(['blocking', 'partial'] as const)('%s: no box of its own, a rule above instead', (impact) => {
    render(<ErrorNotice impact={impact} nested message="Could not save" testId="n" noHandoffReason="unit test" />)
    const n = screen.getByTestId('n')
    expect(n.className).not.toMatch(/\brounded-lg\b/)
    expect(n.className).not.toMatch(/\bbg-(danger|warn)-subtle\b/)
    expect(n.className).toMatch(/\bborder-t\b/)
  })

  it('a nested blocking notice keeps role="alert" and uses the nested inks', () => {
    render(<ErrorNotice nested title="Not saved" message="Read-only." testId="n" noHandoffReason="unit test" />)
    expect(screen.getByTestId('n')).toHaveAttribute('role', 'alert')
    expect(screen.getByText('Not saved').className).toContain('text-danger-ink-nested')
    expect(screen.getByText('Read-only.').className).toContain('text-danger-body-nested')
  })
})

describe('ErrorNotice actions', () => {
  it('renders host buttons in the action row before Ask the agent, which stays a link', () => {
    const onClick = vi.fn()
    render(
      <ErrorNotice
        impact="partial"
        message="Could not read your balance."
        testId="n"
        askAgent
        actions={<button type="button" onClick={onClick}>Refresh</button>}
      />,
    )
    const refresh = screen.getByRole('button', { name: 'Refresh' })
    const ask = screen.getByRole('button', { name: /ask the agent/i })
    const row = refresh.parentElement!
    expect(row).toHaveAttribute('data-notice-actions')
    expect(row.contains(ask)).toBe(true)
    // Host button first, the hand-off after it.
    expect(refresh.compareDocumentPosition(ask) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The host button gets the real-button skin from the row; the link does not.
    expect(row.className).toContain('[&>button]:border-notice-btn-border')
    expect(row.className).toContain('[&>button:focus-visible]:outline-notice-ring')
    expect(ask.parentElement).not.toBe(row)
    expect(ask.className).toContain('underline')
    refresh.click()
    expect(onClick).toHaveBeenCalledOnce()
  })

  it('renders no action row when there is nothing to put in it', () => {
    render(<ErrorNotice message="x" testId="n" noHandoffReason="unit test" />)
    expect(screen.getByTestId('n').querySelector('[data-notice-actions]')).toBeNull()
  })
})
