/**
 * "Crew board" — the work-item board's entry point in the session menu.
 *
 * The contract worth locking is visibility, not navigation: the board is keyed
 * on a conductor, so the entry must not exist for a session that conducts
 * nothing. That is the common case, and a dead entry leading to an empty page
 * is exactly what SendToInstanceSubmenu's self-hiding contract avoids.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  crewBoard: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

/** Where the item asked to go. A spy rather than a rendered route, because the
 *  contract under test is the URL it builds, not what that URL renders. */
const navigated: string[] = []
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useNavigate: () => (to: string) => {
    navigated.push(to)
  },
}))

import CrewBoardMenuItem from '../components/CrewBoardMenuItem'

/** Plain Item stub — jsdom cannot drive a real Radix menu, so the row list is
 *  rendered against a button, the same way InstanceSendItems is tested. */
function ItemStub({ onSelect, children }: {
  readonly onSelect?: (e: Event) => void
  readonly children?: React.ReactNode
}) {
  return <button onClick={() => onSelect?.(new Event('select'))}>{children}</button>
}

function renderItem(slotKey = 'chat-1750-1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CrewBoardMenuItem slotKey={slotKey} Item={ItemStub} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CrewBoardMenuItem', () => {
  beforeEach(() => {
    mocks.crewBoard.mockReset()
    navigated.length = 0
  })

  it('renders no menu entry for a session that owns no work ledger', async () => {
    // What /api/crew-board answers for a session with no ledger: 404 no_ledger.
    mocks.crewBoard.mockRejectedValue(Object.assign(new Error('no_ledger'), { status: 404 }))
    renderItem()
    // Awaiting the rejection first, so this asserts a settled absence rather
    // than merely catching the query still in flight.
    await expect(mocks.crewBoard.mock.results[0]?.value).rejects.toThrow()
    expect(screen.queryByText('Crew board')).toBeNull()
  })

  it('renders the entry for a conductor session, linking to its own board', async () => {
    mocks.crewBoard.mockResolvedValue({
      conductor: 'chat-1750-1', goal: 'ship it', round: 2, items: [],
      channels_available: false,
    })
    renderItem()
    expect(await screen.findByText('Crew board')).toBeTruthy()
  })

  it('surfaces a non-404 failure instead of hiding the entry', async () => {
    // The distinction that matters: 404 means "this session has no board", any
    // other failure means "we could not find out". Hiding on both turns a 500 or
    // a dropped connection into a confident wrong answer the reader cannot question.
    mocks.crewBoard.mockRejectedValue(Object.assign(new Error('gateway exploded'), { status: 500 }))
    renderItem()
    expect(await screen.findByText('Crew board')).toBeTruthy()
    expect(screen.getByText(/gateway exploded/i)).toBeTruthy()
  })

  it('opens that session own board when the entry is chosen', async () => {
    mocks.crewBoard.mockResolvedValue({
      conductor: 'chat-1750-1', goal: 'ship it', round: 2, items: [],
    })
    renderItem('chat-1750-1')
    await userEvent.click(await screen.findByRole('button', { name: /Crew board/ }))
    // The conductor is carried in the query string, encoded: a slot key is opaque
    // and a raw one would break the route for any key needing escaping.
    expect(navigated).toEqual(['/crew-board?conductor=chat-1750-1'])
  })

  it('a click on the failure message does not activate the row', async () => {
    // The message sits inside the row, so without suppression a click meant to read
    // it would also fire the row and replace the error with a fresh attempt.
    mocks.crewBoard.mockRejectedValue(Object.assign(new Error('nope'), { status: 500 }))
    renderItem()
    await userEvent.click(await screen.findByText(/nope/i))
    expect(navigated).toEqual([])
  })
})
