import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef, useState, useEffect } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'
import type { SendMode } from '../pages/chat/ChatSettings'

/**
 * THE DEFECT: on an in-flight zero-match the menu returned `null` while
 * useListKeyboardNav's count===0 arm preventDefaults Enter and Tab at document
 * capture — a keyboard capture with nothing on screen at all, the third and
 * last consumer of `releaseKeysWhenEmpty` to hold keys silently.
 *
 * The settled zero-match already announced ("No matching commands — Enter sends
 * the message"), so only the in-flight window was mute. This is ANNOUNCE-only:
 * the swallow, the release gate and the one-effect-flush lag guard are
 * unchanged, and the negative control below pins that.
 */

function Harness({ input, sendOnEnter, onSelect = vi.fn(), onClose = vi.fn(), anchorTop }: {
  input: string
  sendOnEnter?: SendMode
  onSelect?: (c: string) => void
  onClose?: () => void
  anchorTop?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  // The menu returns null until anchorRef.current exists, and a never-settling
  // query supplies no re-render of its own — so force one after mount.
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
        <SlashCommandMenu
          input={input} anchorRef={ref} open
          onSelect={onSelect} onClose={onClose} sendOnEnter={sendOnEnter}
        />
      </div>
    </QueryClientProvider>
  )
}

/** A gateway that never answers: holds the menu in its isFetching window. */
const neverSettles = () => new Promise<never>(() => {})

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SlashCommandMenu — the in-flight hold announces itself', () => {
  it('pins the menu by its bottom edge so the announcement cannot cover the composer', async () => {
    // Deliberate cross-picker change: all three menus place an opens-above menu
    // by `bottom`, because the base's row-height estimate under-reads reality.
    mockApi.slashCommands.mockImplementation(neverSettles)
    render(<Harness input="/zzq" anchorTop={500} />)
    await screen.findByRole('status')
    const portal = screen.getByRole('listbox')
    expect(portal.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    expect(portal.style.top).toBe('')
  })

  it('renders a live region instead of nothing while the command list loads', async () => {
    mockApi.slashCommands.mockImplementation(neverSettles)
    render(<Harness input="/zzq" />)
    const live = await screen.findByRole('status')
    expect(within(live).getByText(/Loading commands…/)).toBeInTheDocument()
  })

  it('names Enter as held and Escape as the keyboard way out', async () => {
    mockApi.slashCommands.mockImplementation(neverSettles)
    render(<Harness input="/zzq" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Loading commands… — Enter won’t send yet; press Esc, then Enter sends the message')
  })

  it('names Ctrl+Enter instead when that is the send binding', async () => {
    // A bare Enter is a newline in 'ctrl-enter' mode, so naming plain Enter as
    // the held send key would be false — same constraint as the settled copy.
    mockApi.slashCommands.mockImplementation(neverSettles)
    render(<Harness input="/zzq" sendOnEnter="ctrl-enter" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Loading commands… — Ctrl+Enter won’t send yet; press Esc, then Ctrl+Enter sends the message')
  })

  it('NEGATIVE CONTROL: still swallows Enter and Tab while the list is in flight', async () => {
    // Passes on base AND fix — deliberately not the thing under test: it pins
    // this change as announce-only, leaving the #5029 guard exactly as it was.
    mockApi.slashCommands.mockImplementation(neverSettles)
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness input="/zzq" onSelect={onSelect} onClose={onClose} />)
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalled())
    // fireEvent returns false when a listener called preventDefault().
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    expect(onSelect).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the announced escape hatch is real: Escape closes where Enter and Tab do not', async () => {
    // Pins the copy's honesty — the hook's Escape branch runs before the
    // count===0 swallow, and the composer's onClose detaches the listener.
    mockApi.slashCommands.mockImplementation(neverSettles)
    const onClose = vi.fn()
    render(<Harness input="/zzq" onClose={onClose} />)
    await screen.findByRole('status')
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('a settled zero-match still promises the send, not the hold', async () => {
    // The two arms must not be confused: once the fetch settles the gate opens
    // and Enter really does send, so the held copy would then be false.
    mockApi.slashCommands.mockResolvedValue([{ name: '/aa' }])
    render(<Harness input="/zzq" />)
    // findByRole would resolve on the LOADING region first, so wait for the
    // settled text rather than for the region's mere existence.
    await waitFor(() => expect(screen.getByRole('status'))
      .toHaveTextContent('No matching commands — Enter sends the message'))
  })
})
