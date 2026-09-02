/**
 * Tests for the pieces both AWS Control surfaces share (`./shared`).
 *
 * `CopyBtn` is the interesting one: its whole job is a side effect (write to the
 * clipboard) plus a confirmation that has to go away again, and it deliberately
 * SWALLOWS a clipboard rejection - a browser that denies clipboard access must
 * not throw out of an onClick, because the id it copies is selectable by hand
 * anyway. Each of those three behaviours is easy to break silently, so each is
 * pinned here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { CopyBtn, SectionHeader } from './shared'

function withClipboard(writeText: () => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn(writeText) },
    configurable: true,
    writable: true,
  })
  return navigator.clipboard.writeText as unknown as ReturnType<typeof vi.fn>
}

describe('CopyBtn', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('writes the exact text it was given and confirms, then reverts', async () => {
    const writeText = withClipboard(() => Promise.resolve())
    renderWithProviders(<CopyBtn text="217681647555" testId="copy-id" />)

    const btn = screen.getByTestId('copy-id')
    expect(btn).toHaveTextContent(i18nT('apps.awsControl.console.copy'))

    fireEvent.click(btn)
    // The account id, verbatim - a trimmed or reformatted id would paste wrong.
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('217681647555'))
    await waitFor(() => expect(btn).toHaveTextContent(i18nT('apps.awsControl.console.copied')))

    // The confirmation is transient: it must return to the idle label, or the
    // button claims a copy that happened a long time ago.
    vi.advanceTimersByTime(1600)
    await waitFor(() => expect(btn).toHaveTextContent(i18nT('apps.awsControl.console.copy')))
  })

  it('swallows a clipboard rejection instead of throwing out of the click', async () => {
    // Runs from an onClick with no catch, so a rethrow becomes an unhandled
    // rejection that tells the user nothing. The label must simply stay idle.
    const writeText = withClipboard(() => Promise.reject(new Error('denied')))
    renderWithProviders(<CopyBtn text="abc" testId="copy-id" />)

    const btn = screen.getByTestId('copy-id')
    fireEvent.click(btn)

    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(btn).toHaveTextContent(i18nT('apps.awsControl.console.copy'))
    expect(btn).not.toHaveTextContent(i18nT('apps.awsControl.console.copied'))
  })

  it('carries the accessible name it is given, since the label is an icon plus a verb', async () => {
    withClipboard(() => Promise.resolve())
    renderWithProviders(<CopyBtn text="b" testId="copy-id" ariaLabel="Copy account id" />)
    expect(screen.getByTestId('copy-id')).toHaveAttribute('aria-label', 'Copy account id')
  })
})

describe('SectionHeader', () => {
  it('renders its title, icon and actions', () => {
    renderWithProviders(
      <SectionHeader
        icon={<span data-testid="icon" />}
        title="Files"
        actions={<button type="button" data-testid="action">Upload</button>}
      />,
    )
    expect(screen.getByText('Files')).toBeTruthy()
    expect(screen.getByTestId('icon')).toBeTruthy()
    expect(screen.getByTestId('action')).toBeTruthy()
  })

  it('omits the actions slot when there is nothing to put in it', () => {
    renderWithProviders(<SectionHeader icon={<span data-testid="icon" />} title="Backup" />)
    expect(screen.getByText('Backup')).toBeTruthy()
    expect(screen.queryByTestId('action')).toBeNull()
  })
})
