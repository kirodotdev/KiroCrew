/* The Bots panel's starred rows: first in the list, in the left dock's order,
 * under either sort, and moved among themselves by a drag or Alt+Arrow through
 * the dock's own writes, so the panel and the dock show one order. The other
 * rows keep the held order below them. Same api mock shape as
 * MembersPage.filters.test.tsx; the crew update and roster reads answer from
 * one server copy, so a re-read shows what the writes stored. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import type { ComponentProps } from 'react'
import type { DndContext, DragEndEvent } from '@dnd-kit/core'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
    memberThread: vi.fn((slug: string) =>
      Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true }),
    ),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    updateKirocrewAgent: vi.fn(),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    autonudgeForSlot: vi.fn(() => Promise.resolve({ enabled: true, loop: null })),
    monitorForSlot: vi.fn(() => Promise.resolve({ enabled: true, monitor: null })),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

// Keep the real drag context; deliver drops at its callback, since the test
// DOM has no layout for dnd-kit to measure (matesDock.test.tsx does the same).
const dnd = vi.hoisted(() => ({ props: null as ComponentProps<typeof DndContext> | null, rows: new Map<string, unknown>() }))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: ComponentProps<typeof DndContext>) => {
      dnd.props = props
      return <actual.DndContext {...props} />
    },
  }
})
// What each row asks of dnd-kit: collision itself needs a layout the test DOM
// does not have, so the rows' drag and drop switches are read where they are set.
vi.mock('@dnd-kit/sortable', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/sortable')>()
  return {
    ...actual,
    useSortable: (args: Parameters<typeof actual.useSortable>[0]) => {
      dnd.rows.set(String(args.id), args.disabled)
      return actual.useSortable(args)
    },
  }
})

import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { DockMatesSection } from '../../shell/nav/matesDock'
import MembersPage from './MembersPage'
import { NO_TEAM_ID } from './teamGroups'

type Mock = ReturnType<typeof vi.fn>

function row(name: string, overrides: Record<string, unknown> = {}) {
  return { name, slug: name, slot_key: '', running: false, kiro_agent: name, source: 'kirocrew', starred: false, ...overrides }
}

/** Three starred Bots, two placed out of name and recency order and one never
 *  placed (star_order 0), and two unstarred ones. The dock lists the starred
 *  three as dune (1), atlas (2), comet. */
const ROSTER = [
  row('atlas', { starred: true, star_order: 2, last_active_ts: 100 }),
  row('bolt', { last_active_ts: 400 }),
  row('comet', { starred: true, star_order: 0, last_active_ts: 300 }),
  row('dune', { starred: true, star_order: 1, last_active_ts: 50 }),
  row('ember', { last_active_ts: 500 }),
]

/** The server's copy of the roster: the crew update writes it, as the gateway
 *  does (an unstar clears `star_order`), and every roster read returns it. */
let server: ReturnType<typeof row>[] = []

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  dnd.props = null
  dnd.rows.clear()
  ;(api.members as Mock).mockImplementation(() => Promise.resolve({ members: server.map((r) => ({ ...r })) }))
  ;(api.updateKirocrewAgent as Mock).mockImplementation((name: string, body: Record<string, unknown>) => {
    server = server.map((r) =>
      r.name !== name ? r : { ...r, ...body, ...(body.starred === false ? { star_order: 0 } : {}) },
    )
    return Promise.resolve({ ok: true })
  })
})

async function renderPage(rows = ROSTER, { dock = false } = {}) {
  server = rows.map((r) => ({ ...r }))
  const utils = renderWithProviders(
    <>
      <MembersPage />
      {dock && <DockMatesSection collapsed={false} isMobile={false} />}
    </>,
  )
  await within(await screen.findByTestId('member-roster')).findByText(rows[0].name)
  return utils
}

const names = () =>
  Array.from(document.querySelectorAll<HTMLElement>('[data-member-row]')).map((el) => el.dataset.memberRow)
const dockNames = () =>
  screen.queryAllByTestId(/^dock-mate-/).map((el) => el.getAttribute('data-testid')!.replace('dock-mate-', ''))
const rowButton = (name: string) => document.querySelector<HTMLButtonElement>(`[data-member-row="${name}"]`)!
const writes = () => (api.updateKirocrewAgent as Mock).mock.calls
const altArrow = (name: string, key: 'ArrowUp' | 'ArrowDown') => fireEvent.keyDown(rowButton(name), { key, altKey: true })

async function openFilters() {
  fireEvent.keyDown(screen.getByTestId('member-filter-menu'), { key: 'Enter' })
  await screen.findByTestId('member-filter-starred')
}

/** A drop as dnd-kit reports it: each side carries its group's id. */
function drop(active: string, over: string, overGroup = NO_TEAM_ID) {
  const side = (id: string, containerId: string) => ({ id, data: { current: { sortable: { containerId } } } })
  act(() => dnd.props!.onDragEnd!({ active: side(active, NO_TEAM_ID), over: side(over, overGroup) } as unknown as DragEndEvent))
}

describe('Bots panel starred order', () => {
  it('lists the starred Bots first, in the dock order, under both sorts', async () => {
    await renderPage(ROSTER, { dock: true })
    expect(names()).toEqual(['dune', 'atlas', 'comet', 'ember', 'bolt'])
    expect(dockNames()).toEqual(['dune', 'atlas', 'comet'])
    await openFilters()
    fireEvent.click(screen.getByTestId('member-sort-name'))
    expect(names()).toEqual(['dune', 'atlas', 'comet', 'bolt', 'ember'])
    // The Starred filter lists the dock's rows, in the dock's order.
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    expect(names()).toEqual(['dune', 'atlas', 'comet'])
  })

  it('Alt+ArrowDown moves a starred row through the dock\'s writes, and the dock shows the same order', async () => {
    await renderPage(ROSTER, { dock: true })
    rowButton('dune').focus()
    altArrow('dune', 'ArrowDown')
    // A browser drops the focus of a row React re-inserts lower in the list;
    // the test DOM keeps it, so drop it here as the browser does.
    rowButton('dune').blur()
    await waitFor(() => expect(names()).toEqual(['atlas', 'dune', 'comet', 'ember', 'bolt']))
    expect(dockNames()).toEqual(['atlas', 'dune', 'comet'])
    // Every starred row renumbered, in order, as a dock drag writes them.
    await waitFor(() => expect(screen.getByTestId('dock-mates')).toHaveAttribute('aria-busy', 'false'))
    expect(writes()).toEqual([['atlas', { star_order: 1 }], ['dune', { star_order: 2 }], ['comet', { star_order: 3 }]])
    // The re-read after the save shows the stored order, and focus stayed on the moved row.
    expect(api.members).toHaveBeenCalledTimes(2)
    expect(names()).toEqual(['atlas', 'dune', 'comet', 'ember', 'bolt'])
    expect(document.activeElement).toBe(rowButton('dune'))
    // At the top the chord is still the row's, and moves nothing.
    expect(altArrow('atlas', 'ArrowUp')).toBe(false)
    // An unstarred row leaves the chord to the page (the open chat's turn step).
    expect(altArrow('ember', 'ArrowDown')).toBe(true)
    expect(writes()).toHaveLength(3)
  })

  it('with a search typed, Alt+Arrow passes the next starred row the list shows', async () => {
    await renderPage()
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'e' } })
    expect(names()).toEqual(['dune', 'comet', 'ember'])
    altArrow('dune', 'ArrowDown')
    await waitFor(() => expect(names()).toEqual(['comet', 'dune', 'ember']))
    // Renumbered over the whole starred list, the hidden atlas included.
    await waitFor(() => expect(writes()).toHaveLength(3))
    expect(writes()).toEqual([['atlas', { star_order: 1 }], ['comet', { star_order: 2 }], ['dune', { star_order: 3 }]])
  })

  it('a drop puts the dragged row in the target row\'s place; a drop on another group\'s row writes nothing', async () => {
    await renderPage()
    drop('comet', 'dune', 'team-a')
    expect(writes()).toHaveLength(0)
    drop('comet', 'dune')
    await waitFor(() => expect(names()).toEqual(['comet', 'dune', 'atlas', 'ember', 'bolt']))
    await waitFor(() => expect(writes()).toHaveLength(3))
    expect(writes()).toEqual([['comet', { star_order: 1 }], ['dune', { star_order: 2 }], ['atlas', { star_order: 3 }]])
  })

  it('only a starred row is a drag source or a drop target', async () => {
    // An unstarred drop target would take a drop past the starred rows, which
    // changes nothing; without it the last starred row takes the drop.
    await renderPage()
    const on = { draggable: false, droppable: false }
    const off = { draggable: true, droppable: true }
    expect(Object.fromEntries(dnd.rows)).toEqual({ atlas: on, comet: on, dune: on, bolt: off, ember: off })
  })

  it('a star or an unstar moves only that row, and a refetch still moves none', async () => {
    const { queryClient } = await renderPage([
      row('atlas', { starred: true, star_order: 1, last_active_ts: 50 }),
      row('bolt', { last_active_ts: 300 }),
      row('comet', { last_active_ts: 200 }),
      row('dune', { last_active_ts: 100 }),
    ])
    expect(names()).toEqual(['atlas', 'bolt', 'comet', 'dune'])
    // dune's chat advances on the server and a refetch lands it: the held order stands.
    server = server.map((r) => (r.name === 'dune' ? { ...r, last_active_ts: 999 } : r))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
    expect(names()).toEqual(['atlas', 'bolt', 'comet', 'dune'])
    // A star puts comet last among the starred rows.
    fireEvent.click(screen.getByTestId('member-star-comet'))
    await waitFor(() => expect(names()).toEqual(['atlas', 'comet', 'bolt', 'dune']))
    await waitFor(() => expect(screen.getByTestId('member-star-comet')).not.toHaveAttribute('aria-disabled'))
    // An unstar puts it back in its held place, ahead of dune's newer chat.
    fireEvent.click(screen.getByTestId('member-star-comet'))
    await waitFor(() => expect(names()).toEqual(['atlas', 'bolt', 'comet', 'dune']))
  })

  it('says the order did not save when a move fails, and shows the order the server kept', async () => {
    await renderPage()
    ;(api.updateKirocrewAgent as Mock).mockRejectedValueOnce(new Error('write failed'))
    altArrow('dune', 'ArrowDown')
    const notice = await screen.findByTestId('member-reorder-error')
    expect(notice).toHaveTextContent("Couldn't save the new order of your bots.")
    await waitFor(() => expect(names()).toEqual(['dune', 'atlas', 'comet', 'ember', 'bolt']))
  })
})
