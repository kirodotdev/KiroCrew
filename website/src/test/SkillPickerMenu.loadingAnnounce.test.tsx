import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef, useState, useEffect } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({ skills: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillPickerMenu from '../components/SkillPickerMenu'
import type { SendMode } from '../pages/chat/ChatSettings'

/**
 * THE DEFECT: while the fetch is in flight the release gate holds Enter and Tab
 * (useListKeyboardNav's count===0 branch preventDefaults them at document
 * capture), but the loading branch was a plain <div> saying only "Loading
 * skills…". So the keyboard was silently captured for up to SKILLS_TIMEOUT_MS
 * while the send ARROW still worked — the asymmetry rnoack hit — and a
 * screen-reader user got no signal at all, unlike every settled-empty branch,
 * which announces the flip in a role="status" live region.
 *
 * This is an ANNOUNCE-only fix: the swallow is deliberate (releasing Enter while
 * matches are transiently unknowable would irreversibly send a draft whose
 * $token the user was still completing), and the negative control below pins
 * that it is unchanged.
 */

function Harness({ query = '', open = true, onSelect = vi.fn(), onClose = vi.fn(), sendOnEnter, anchorTop }: {
  query?: string; open?: boolean; onSelect?: (i: { leaf: string; key: string }) => void
  onClose?: () => void; sendOnEnter?: SendMode; anchorTop?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
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
        <SkillPickerMenu
          query={query} anchorRef={ref} open={open}
          onSelect={onSelect} onClose={onClose} sendOnEnter={sendOnEnter}
        />
      </div>
    </QueryClientProvider>
  )
}

/** A gateway that never answers: holds the menu in its loading branch. */
const neverSettles = () => () => new Promise(() => {})

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SkillPickerMenu — the loading branch announces that the send key will not send yet', () => {
  it('pins the menu by its bottom edge so the announcement cannot cover the composer', async () => {
    // This picker's declared change is copy, but it shares the geometry helper,
    // so the placement change reaches it too — pinned here rather than implied.
    mockApi.skills.mockImplementation(neverSettles())
    render(<Harness anchorTop={500} />)
    // The positioned element is the OUTER portal; role="listbox" is an inner
    // node here (see the component's own note), so select by the style carrier.
    const portal = (await screen.findByRole('status')).closest('div[style]') as HTMLElement
    expect(portal.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    expect(portal.style.top).toBe('')
  })

  it('renders the loading state as a live region, not a mute div', async () => {
    mockApi.skills.mockImplementation(neverSettles())
    render(<Harness query="grill" />)
    const live = await screen.findByRole('status')
    expect(within(live).getByText(/Loading skills…/)).toBeInTheDocument()
  })

  it('says Enter will not send yet, and names the Send button as what does', async () => {
    mockApi.skills.mockImplementation(neverSettles())
    render(<Harness query="grill" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Loading skills… — Enter won’t send yet; press Esc, then Enter sends the message')
  })

  it('names Ctrl+Enter instead when that is the send binding', async () => {
    // A bare Enter is a newline in 'ctrl-enter' mode, so naming plain Enter as
    // the key that won't send would be false — the settled copy has the same constraint.
    mockApi.skills.mockImplementation(neverSettles())
    render(<Harness query="grill" sendOnEnter="ctrl-enter" />)
    expect(await screen.findByRole('status'))
      .toHaveTextContent('Loading skills… — Ctrl+Enter won’t send yet; press Esc, then Ctrl+Enter sends the message')
  })

  it('NEGATIVE CONTROL: still swallows Enter while loading, and chooses nothing', async () => {
    // Passes on base AND fix — deliberately not the thing under test: it pins
    // that this PR changed the announcement only, leaving the #5029 guard as it was.
    mockApi.skills.mockImplementation(neverSettles())
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="grill" onSelect={onSelect} onClose={onClose} />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    // fireEvent returns false when a listener called preventDefault().
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    expect(onSelect).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})
