import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef, useState, useEffect } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({ fileSearch: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import FilePickerMenu from '../components/FilePickerMenu'
import type { SendMode } from '../pages/chat/ChatSettings'

/**
 * THE DEFECT: none of the picker's three empty branches was a live region, while
 * useListKeyboardNav's count===0 branch preventDefaults Enter/Tab at document
 * capture — so the keyboard was silently captured and only the send ARROW still
 * worked. Two of the branches hold the gate closed by construction: it requires
 * `query.length >= 2`, a settled 200ms debounce and a finished fetch.
 *
 * This is an ANNOUNCE-only fix. The swallow is deliberate — releasing Enter while
 * matches are transiently unknowable would irreversibly send a draft whose
 * @token the user was still completing — and the negative controls below pin it
 * as unchanged.
 */

function Harness({ query, sendOnEnter, onSelect = vi.fn(), onClose = vi.fn(), anchorTop }: {
  query: string
  sendOnEnter?: SendMode
  onSelect?: (i: { path: string; relativePath: string; kind: 'file' | 'dir' }) => void
  onClose?: () => void
  anchorTop?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  // The picker returns null until anchorRef.current exists, and a never-settling
  // fetch supplies no re-render of its own — so force one after mount.
  const [, tick] = useState(0)
  useEffect(() => {
    // jsdom reports an all-zero rect, which reads as "no room above". Stub it so
    // the opens-above branch under test is the one that runs.
    if (anchorTop != null && ref.current) {
      ref.current.getBoundingClientRect = () => ({
        top: anchorTop, left: 40, width: 600, height: 44, bottom: anchorTop + 44,
        right: 640, x: 40, y: anchorTop, toJSON: () => ({}),
      }) as DOMRect
    }
    tick(1)
  }, [anchorTop])
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <FilePickerMenu
          query={query} anchorRef={ref} open
          onSelect={onSelect} onClose={onClose} sendOnEnter={sendOnEnter}
        />
      </div>
    </QueryClientProvider>
  )
}

/** A backend that never answers: holds the menu in its isFetching branch. */
const neverSettles = () => new Promise<never>(() => {})

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('FilePickerMenu — the empty branches announce that Enter will not send yet', () => {
  it('pins the announcing menu by its bottom edge so it cannot cover the composer', async () => {
    // Measured before this fix: the announcement overhung the composer by 7px in
    // English and 25px in German, whose copy wraps to a third line.
    mockApi.fileSearch.mockImplementation(neverSettles)
    render(<Harness query="zz" anchorTop={500} />)
    await screen.findByRole('status')
    const portal = screen.getByRole('listbox')
    expect(portal.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    // A top placement is what let the rendered height decide the bottom edge.
    expect(portal.style.top).toBe('')
  })

  it('renders the in-flight state as a live region, not a mute div', async () => {
    mockApi.fileSearch.mockImplementation(neverSettles)
    render(<Harness query="zz" />)
    const live = await screen.findByRole('status')
    expect(within(live).getByText(/Searching…/)).toBeInTheDocument()
  })

  it('names Enter as held and Escape as the keyboard way out', async () => {
    // Send is NOT the remedy to announce: the hook swallows Tab too, so a
    // keyboard user cannot reach that button while the picker is open.
    mockApi.fileSearch.mockImplementation(neverSettles)
    render(<Harness query="zz" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Searching… — Enter won’t send yet; press Esc, then Enter sends the message')
  })

  it('names Ctrl+Enter instead when that is the send binding', async () => {
    // A bare Enter is a newline in 'ctrl-enter' mode, so naming plain Enter as
    // the held send key would be false — the constraint the settled copy has too.
    mockApi.fileSearch.mockImplementation(neverSettles)
    render(<Harness query="zz" sendOnEnter="ctrl-enter" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Searching… — Ctrl+Enter won’t send yet; press Esc, then Ctrl+Enter sends the message')
  })

  it('announces the held key in the under-2-character state too', async () => {
    // This branch never fetches, so the hold persists for as long as the user
    // leaves the token short — unbounded, unlike the in-flight case.
    render(<Harness query="z" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Type 2+ chars to search files and folders… — Enter won’t send yet; press Esc, then Enter sends the message')
    expect(mockApi.fileSearch).not.toHaveBeenCalled()
  })

  it('NEGATIVE CONTROL: still swallows Enter and Tab while a search is in flight', async () => {
    // Passes on base AND fix — deliberately not the thing under test: it pins
    // this change as announce-only, leaving the #5029 guard exactly as it was.
    mockApi.fileSearch.mockImplementation(neverSettles)
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="zz" onSelect={onSelect} onClose={onClose} />)
    await waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalled())
    // fireEvent returns false when a listener called preventDefault().
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    expect(onSelect).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('NEGATIVE CONTROL: still swallows Enter and Tab below 2 characters', async () => {
    // The evidence for announcing on that branch at all: the hold is real there,
    // not merely an unbound key. Passes on base AND fix.
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="z" onSelect={onSelect} onClose={onClose} />)
    await screen.findByRole('listbox')
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    expect(onSelect).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the announced escape hatch is real: Escape closes while Enter and Tab do not', async () => {
    // Pins the copy's honesty. The hook's Escape branch runs BEFORE the
    // count===0 swallow, so Escape works exactly where Enter and Tab are eaten.
    mockApi.fileSearch.mockImplementation(neverSettles)
    const onClose = vi.fn()
    render(<Harness query="zz" onClose={onClose} />)
    await waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalled())
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('FilePickerMenu — an opens-above menu is placed by its bottom edge', () => {
  /**
   * Scope note, since this reaches beyond the announcement: the placement change
   * is deliberate and covers POPULATED menus too, because the row-height estimate
   * `menuGeometry` is given under-reads the real row box — measured 53px against
   * the 48px passed in, so a 3-row menu overhung its budget by 18px on the base
   * independently of any copy change. Pinning both states here keeps a later
   * reader from trimming the fix back to the empty branch.
   */
  it('pins the bottom edge with rows present, not just on the announcement', async () => {
    mockApi.fileSearch.mockResolvedValue({
      results: [
        { path: '/repo/a.ts', name: 'a.ts', size: 1, mtime: 1, kind: 'file' },
        { path: '/repo/b.ts', name: 'b.ts', size: 1, mtime: 1, kind: 'file' },
      ],
      root: '/repo',
    })
    render(<Harness query="zz" anchorTop={500} />)
    expect(await screen.findByText('a.ts')).toBeInTheDocument()
    const portal = screen.getByRole('listbox')
    expect(portal.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    expect(portal.style.top).toBe('')
  })
})
