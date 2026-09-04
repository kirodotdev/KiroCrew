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
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { fmtBytes } from '../../i18n/format'
import { CopyBtn, PaneHeader, MetricCard, StorageBar, QuickTile } from './shared'
import type { DriveUsage } from './types'

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

describe('PaneHeader', () => {
  it('renders the subtitle under the title, and nothing when there is none', () => {
    const r = renderWithProviders(<PaneHeader title="Overview" subtitle="One room for everything." />)
    expect(screen.getByTestId('page-title')).toHaveTextContent('Overview')
    expect(screen.getByTestId('pane-subtitle')).toHaveTextContent('One room for everything.')
    r.unmount()

    // A pane with no orientation line must not leave an empty paragraph behind
    // it — that reserved gap is what made the panes' spacing drift apart.
    renderWithProviders(<PaneHeader title="Backup" />)
    expect(screen.queryByTestId('pane-subtitle')).toBeNull()
  })

  it('keeps a caller-owned node (and its test id) inside its own type scale', () => {
    // The totals sentence on the accounts pane is addressed by test id, so the
    // slot has to take a node rather than only a string.
    renderWithProviders(
      <PaneHeader title="Accounts" subtitle={<span data-testid="accounts-totals">2 accounts</span>} />,
    )
    const p = screen.getByTestId('pane-subtitle')
    expect(within(p).getByTestId('accounts-totals')).toHaveTextContent('2 accounts')
    expect(p.className).toContain('text-[13px]')
  })
})

describe('MetricCard', () => {
  it('renders the label, the value and the sub-line as one card', () => {
    renderWithProviders(
      <MetricCard label="Accounts" value="2" sub="1 needs attention" testId="m" />,
    )
    const card = screen.getByTestId('m')
    expect(within(card).getByTestId('stat-card-label')).toHaveTextContent('Accounts')
    expect(within(card).getByTestId('stat-card-value')).toHaveTextContent('2')
    expect(screen.getByTestId('m-sub')).toHaveTextContent('1 needs attention')
  })

  it('holds the card skeleton while the value is unknown, so nothing jumps', () => {
    // An undefined value is a query still in flight, not the number zero: the
    // card must reserve its own height rather than render an em-dash and then
    // resize under the reader.
    renderWithProviders(<MetricCard label="Drive used" testId="m" />)
    expect(within(screen.getByTestId('m')).getByTestId('stat-card-skeleton')).toBeTruthy()
    expect(screen.queryByTestId('m-sub')).toBeNull()
  })

  it('omits the sub-line rather than reserving an empty one', () => {
    renderWithProviders(<MetricCard label="Share links" value="0" testId="m" />)
    expect(screen.queryByTestId('m-sub')).toBeNull()
  })
})

function usage(over: Partial<DriveUsage> = {}): DriveUsage {
  return {
    bytes: 4_000_000_000,
    objects: 42,
    sections: {
      drive: { objects: 30, bytes: 2_000_000_000 },
      library: { objects: 10, bytes: 1_000_000_000 },
      backup: { objects: 2, bytes: 1_000_000_000 },
    },
    ...over,
  }
}

describe('StorageBar', () => {
  it('draws one segment per non-empty section and names every section in the legend', () => {
    renderWithProviders(<StorageBar usage={usage()} testId="sb" />)

    // Segments are proportional to bytes, so the widths are the assertion.
    expect(screen.getByTestId('sb-segment-drive').style.width).toBe('50%')
    expect(screen.getByTestId('sb-segment-library').style.width).toBe('25%')
    expect(screen.getByTestId('sb-segment-backup').style.width).toBe('25%')

    // Colour is never the only cue: each swatch is labelled with its section
    // name and its own size.
    const legend = screen.getByTestId('sb-legend-library')
    expect(legend).toHaveTextContent(i18nT('apps.awsControl.console.section_library'))
    expect(legend).toHaveTextContent(fmtBytes(1_000_000_000))
  })

  it('draws the bare track for an empty drive, and still lists every section', () => {
    // Zero bytes must read as "nothing stored yet", not as a failed render —
    // and a 0-byte section keeps its legend row so the sections stay a fixed set.
    renderWithProviders(
      <StorageBar
        usage={usage({
          bytes: 0,
          objects: 0,
          sections: {
            drive: { objects: 0, bytes: 0 },
            library: { objects: 0, bytes: 0 },
            backup: { objects: 0, bytes: 0 },
          },
        })}
        testId="sb"
      />,
    )
    expect(screen.queryByTestId('sb-segment-drive')).toBeNull()
    expect(screen.getByTestId('sb-legend-drive')).toBeTruthy()
    expect(screen.getByTestId('sb-legend-library')).toBeTruthy()
    expect(screen.getByTestId('sb-legend-backup')).toBeTruthy()
  })
})

describe('QuickTile', () => {
  it('renders its label and count, with the count addressable on its own', () => {
    renderWithProviders(
      <QuickTile icon={<span data-testid="tile-icon" />} label="Files" count="128 items" testId="qt" />,
    )
    expect(screen.getByTestId('qt')).toHaveTextContent('Files')
    expect(screen.getByTestId('qt-count')).toHaveTextContent('128 items')
    expect(screen.getByTestId('tile-icon')).toBeTruthy()
  })
})
