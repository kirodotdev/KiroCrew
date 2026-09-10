/**
 * "Export to a file" — the session menu's file-export row.
 *
 * The row is rendered against a plain `Item` stub rather than a live Radix menu:
 * jsdom cannot drive a Radix submenu open (no PointerEvent), the same limitation
 * SendToInstanceSubmenu.test.tsx documents, and what matters here is the
 * disabled / outcome logic rather than the menu shell.
 *
 * Three contracts are locked:
 *   (1) an incognito or temporary session cannot be exported, and the row SAYS
 *       so instead of offering a click the backend only ever refuses;
 *   (2) the menu stays open and the outcome lands on the row, because a
 *       download's only visible effect is in the browser's own download surface;
 *   (3) a refusal surfaces the endpoint's own message rather than a generic one.
 */
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({ exportSession: vi.fn() }))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import ExportSessionItem from '../components/ExportSessionItem'

/** A plain stand-in for the Radix menu-item primitive. */
function StubItem({ title, disabled, onSelect, children }: {
  title?: string
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      data-testid="row"
      onClick={() => onSelect?.(new Event('select'))}
    >
      {children}
    </button>
  )
}

function renderRow(memoryMode?: 'persistent' | 'incognito' | 'temporary') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ExportSessionItem slotKey="slot-1" Item={StubItem} memoryMode={memoryMode} />
    </QueryClientProvider>,
  )
}

function row() {
  return screen.getByTestId('row') as HTMLButtonElement
}

describe('ExportSessionItem', () => {
  beforeEach(() => {
    mocks.exportSession.mockReset()
    mocks.exportSession.mockResolvedValue(undefined)
  })

  it('offers the export for a persistent session', () => {
    renderRow('persistent')
    expect(row().disabled).toBe(false)
    expect(screen.getByText('Export to a file')).toBeTruthy()
  })

  it('refuses an incognito session on the row, with the reason', () => {
    renderRow('incognito')
    expect(row().disabled).toBe(true)
    expect(screen.getByText('session not saved to disk')).toBeTruthy()
  })

  it('refuses a temporary session the same way', () => {
    renderRow('temporary')
    expect(row().disabled).toBe(true)
    expect(screen.getByText('session not saved to disk')).toBeTruthy()
  })

  it('does not call the endpoint for a session it will not export', () => {
    renderRow('incognito')
    row().click()
    expect(mocks.exportSession).not.toHaveBeenCalled()
  })

  it('exports the slot it was given and reports the outcome on the row', async () => {
    renderRow('persistent')
    row().click()
    await waitFor(() => expect(mocks.exportSession).toHaveBeenCalledWith('slot-1'))
    await waitFor(() => expect(screen.getByText('Exported')).toBeTruthy())
  })

  it('keeps the menu open on select, so the row can report at all', () => {
    // preventDefault on the select event is what holds a Radix menu open. Without
    // it the menu closes and a refusal has nowhere left to render.
    const seen: Event[] = []
    function Recorder({ onSelect }: { onSelect?: (e: Event) => void }) {
      return (
        <button type="button" aria-label="export" data-testid="row" onClick={() => {
          const e = new Event('select', { cancelable: true })
          seen.push(e)
          onSelect?.(e)
        }} />
      )
    }
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ExportSessionItem slotKey="slot-1" Item={Recorder} memoryMode="persistent" />
      </QueryClientProvider>,
    )
    row().click()
    expect(seen).toHaveLength(1)
    expect(seen[0].defaultPrevented).toBe(true)
  })

  it("surfaces the endpoint's own refusal message as readable text", async () => {
    // Not in a `title=` attribute: the failure has to be reachable by a keyboard
    // or touch user, which is what the shared ErrorNotice guarantees.
    mocks.exportSession.mockRejectedValue(new Error('this session has no messages to export'))
    renderRow('persistent')
    row().click()
    await waitFor(() => expect(screen.getByText('Failed')).toBeTruthy())
    expect(screen.getByText('this session has no messages to export')).toBeTruthy()
    expect(screen.getByRole('alert')).toBeTruthy()
  })

  it('falls back to a readable message when the failure carries none', async () => {
    mocks.exportSession.mockRejectedValue(new Error(''))
    renderRow('persistent')
    row().click()
    await waitFor(() => expect(screen.getByText('The export failed')).toBeTruthy())
  })

  it('does not re-fire the export when the error surface is clicked', async () => {
    // The notice lives INSIDE a menu item, so a click on it would otherwise bubble
    // to the item and activate it -- replacing the error with a fresh spinner.
    mocks.exportSession.mockRejectedValue(new Error('this session has no messages to export'))
    renderRow('persistent')
    row().click()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(mocks.exportSession).toHaveBeenCalledTimes(1)

    screen.getByRole('alert').click()
    expect(mocks.exportSession).toHaveBeenCalledTimes(1)
  })

  it('offers the agent hand-off, because an export has no unsaved draft to lose', async () => {
    // `errors-use-error-notice` mandates the hand-off on a surface whose inputs are
    // already persisted, which an export is: the navigation destroys nothing.
    mocks.exportSession.mockRejectedValue(new Error('this session has no messages to export'))
    renderRow('persistent')
    row().click()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByRole('alert').querySelectorAll('button').length).toBeGreaterThan(0)
  })

  it('offers the export when the memory mode is not known yet', () => {
    // An undefined mode means the slot has not loaded, not that it is restricted;
    // hiding the action there would make the feature look missing.
    renderRow(undefined)
    expect(row().disabled).toBe(false)
  })
})
