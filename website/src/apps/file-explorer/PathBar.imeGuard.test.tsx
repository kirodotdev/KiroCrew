/**
 * IME guard on the Files path bar's Tab accept — the site-level case
 * `test/ImeEnterGuardSites.test.tsx` pins for the core inputs.
 *
 * On WebKit the keydown that commits an IME candidate is dispatched AFTER
 * `compositionend`, where `KeyboardEvent.isComposing` already reads false. The
 * hook's 50ms latch is the only signal that survives into that window, so each
 * test arms the latch with compositionStart→compositionEnd and then fires the
 * committing key exactly as WebKit delivers it (native flag clear).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('./api', () => ({
  fileExplorerApi: {
    complete: vi.fn().mockResolvedValue({
      entries: [{ name: 'src', path: '/home/user/src', type: 'dir', size: 0, mtime: 0 }],
    }),
  },
}))

import PathBar from './PathBar'
import { fileExplorerApi } from './api'

/** Arm the hook's post-composition latch: the WebKit commit-key window. */
function armLatch(el: Element) {
  fireEvent.compositionStart(el)
  fireEvent.compositionEnd(el)
}

describe('file-explorer PathBar — Tab accepts a suggestion INTO the draft', () => {
  // The path bar recorded its Tab as a deliberate exemption ("arrow/Tab
  // navigation stays untouched"), which held only while Tab did nothing to the
  // text. It accepts the highlighted suggestion into the draft, so with the
  // latch armed the accept overwrote the composing path.
  async function openEditor() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <PathBar rootPath="/home/user" gitInfo={null} onChangeRoot={vi.fn()} onNavigate={vi.fn()} />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByTitle('Click to edit path'))
    const input = screen.getByPlaceholderText('/path/to/folder') as HTMLInputElement
    fireEvent.change(input, { target: { value: '/home/user/s' } })
    // Establish the state Tab is asserted against, not merely "a row is showing".
    // The `complete` mock answers every key with the same entry, so the row
    // first renders for the PRE-debounce key (`/home/user`, fetched on focus).
    // 150ms later the debounced draft flips the query key, `data` resets to
    // undefined and the list is EMPTY for a tick until the new fetch resolves --
    // and a Tab that lands in that tick finds no suggestions, so neither the
    // accept nor the latch branch runs (both PathBar cases red in one of four
    // loaded runs, with the draft untouched and default not prevented). Wait for
    // the debounced key's fetch to have been issued, THEN for its row: a row seen
    // after that call can only be the settled list.
    const complete = vi.mocked(fileExplorerApi.complete)
    await waitFor(() => expect(complete).toHaveBeenCalledWith('/home/user/s', 'dir', 30), { timeout: 5000 })
    // The fetch itself is a debounced (150ms) query, so the row can still arrive
    // after the default waitFor timeout (1000ms) under a loaded, concurrent run --
    // a named ceiling for that chain (same class as DiffBlock.streaming.test.tsx /
    // PierreWorkspaceTree.lazy.test.tsx).
    await waitFor(() => expect(screen.getByText('/home/user/src')).toBeInTheDocument(), { timeout: 5000 })
    return input
  }

  it('does NOT replace the draft on the committing Tab in the post-composition window', async () => {
    const input = await openEditor()
    armLatch(input)
    const defaultNotPrevented = fireEvent.keyDown(input, { key: 'Tab' })
    expect(input.value).toBe('/home/user/s')
    expect(defaultNotPrevented).toBe(false)
  })

  it('accepts the highlighted suggestion on a plain Tab (positive control)', async () => {
    const input = await openEditor()
    fireEvent.keyDown(input, { key: 'Tab' })
    expect(input.value).toBe('/home/user/src')
  })

  it('keeps arrow-key list navigation working while the latch is armed', async () => {
    const input = await openEditor()
    armLatch(input)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(screen.getByRole('option')).toHaveAttribute('aria-selected', 'true')
  })
})
