/**
 * The crew-log side-panel section: what it renders from the five folds, and the
 * one behaviour that is not visible in the markup — the re-read on the end of a
 * turn.
 *
 * The section is a READER. So the properties worth pinning are the ones a reader
 * can get wrong: it must not invent a number the fold did not report, it must
 * report a fold's own `seq` rather than a count of entries, and it must tell
 * "nothing recorded" apart from "nothing fetched yet".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { CrewLogTab, CREW_LOG_FOLDS } from '../pages/chat/CrewLogPanel'
import { api } from '../api/client'
import { setActiveSlot, setSlotState, sseSubagentPending, syncSlotRunningFromServer } from '../store/chatSlice'

const SLOT = 'chat-1'

/** A bundle at seq 0 with every fold empty — what a session with no crew log
 *  reads back, because the route answers an empty fold rather than a 404. */
function emptyBundle() {
  return Object.fromEntries(
    CREW_LOG_FOLDS.map(name => [name, { name, seq: 0, value: {} }]),
  )
}

/** The client's shape: the folds plus the two facts the folds cannot carry --
 *  whether a unit was addressable, and whether the writer had drained. Tests that
 *  care about either pass it explicitly. */
function read(folds: unknown, over: { resolved?: boolean; writesDrained?: boolean } = {}) {
  return { folds, resolved: true, writesDrained: true, ...over } as never
}

function populatedBundle(overrides: Record<string, unknown> = {}) {
  return {
    status: {
      name: 'status',
      seq: 1842,
      value: {
        lifecycle: 'open',
        agent: 'kirocrew',
        model: 'a-model',
        opened_at: 1789821630000,
        turns_completed: 12,
        turns_refused: 0,
        turn_open: false,
        last_stop_reason: 'end_turn',
        entries: 1842,
        dropped: { count: 0, bytes: 0 },
      },
    },
    usage: {
      name: 'usage',
      seq: 1842,
      value: {
        turns: { completed: 12, credits_reported: 12, tokens_reported: 12, duration_reported: 12 },
        credits: 8.41,
        tokens: { input: 918220, output: 96431, cache_read: 182004, cache_write: 8258, total: 1204913 },
        duration_ms: 1624000,
        by_model: { 'a-model': { turns: 12, credits: 8.41, credits_reported: 12, tokens: 1204913 } },
        models_omitted: 0,
        context: { tokens: 42000, chars: 168000, blocks: 9, estimated_turns: 0, by_source: {} },
        compactions: { count: 3, freed_pct: 41 },
        steps: { completed: 40, ms: 900 },
      },
    },
    timeline: {
      name: 'timeline',
      seq: 1842,
      value: {
        moments: [
          { seq: 1, time: 1789821630000, type: 'session/opened' },
          { seq: 1834, time: 1789822918000, type: 'turn/completed' },
          { seq: 1838, time: 1789822923000, type: 'turn/started' },
        ],
        dropped: 14,
        limit: 200,
        first_seq: 1,
        last_seq: 1838,
      },
    },
    tools: {
      name: 'tools',
      seq: 1840,
      value: {
        calls: 96,
        completed: 94,
        errors: 4,
        open: 2,
        open_calls: [{ call_id: 'c-1', name: 'execute_bash', time: 1789822921000, seq: 1840 }],
        open_calls_omitted: 0,
        elapsed_ms: 74000,
        by_name: {
          execute_bash: { calls: 28, completed: 25, errors: 3, elapsed_ms: 61400, last_status: 'ok', last_time: '', servers: [], servers_omitted: 0 },
          fs_read: { calls: 41, completed: 41, errors: 0, elapsed_ms: 9120, last_status: 'ok', last_time: '', servers: [], servers_omitted: 0 },
        },
        names_omitted: 0,
      },
    },
    approvals: {
      name: 'approvals',
      seq: 1842,
      value: {
        requested: 4,
        decided: 3,
        pending: 1,
        pending_requests: [{ approval_id: 'a-1', tool: 'execute_bash', reason: '', turn: 12, time: 1789822922000, seq: 1841 }],
        pending_omitted: 0,
        by_decision: { approved: 2, rejected_once: 1 },
        last: null,
      },
    },
    ...overrides,
  }
}

describe('CrewLogTab', () => {
  beforeEach(() => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(populatedBundle()))
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders each fold with a summary a collapsed header can be read from', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Status')).toBeInTheDocument())
    // Every fold has a header, whether or not its body is open.
    for (const title of ['Status', 'Usage', 'Timeline', 'Tools', 'Approvals']) {
      expect(screen.getByText(title)).toBeInTheDocument()
    }
    // The collapsed list folds still say how much they hold.
    expect(screen.getByText('moments: 3')).toBeInTheDocument()
    expect(screen.getByText('calls: 96 · unfinished: 2')).toBeInTheDocument()
    expect(screen.getByText('asked: 4 · pending: 1')).toBeInTheDocument()
  })

  it('reports the highest fold seq as the version it folded through', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    // 1842 is the newest of the five; the tools fold sits at 1840 because it was
    // read a moment earlier, and reporting the LOWEST would understate the value
    // on screen while reporting an entry count would not be a version at all.
    await waitFor(() => expect(screen.getByText('up to date through entry 1,842')).toBeInTheDocument())
  })

  it('opens Status and Usage, and opens a list fold on click', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Lifecycle')).toBeInTheDocument())
    // Status body is open by default …
    expect(screen.getByText('completed: 12 · refused: 0')).toBeInTheDocument()
    // … the timeline body is not.
    expect(screen.queryByText('turn started')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Timeline'))
    expect(screen.getByText('turn started')).toBeInTheDocument()
    // Newest first: the most recent moment renders before the session's opener.
    // The record's own vocabulary is worded here, so the rows read as sentences
    // rather than as slash-separated entry types.
    const types = screen.getAllByText(/^(session opened|turn started|turn finished)$/).map(n => n.textContent)
    expect(types).toEqual(['turn started', 'turn finished', 'session opened'])
  })

  it('shows a dash rather than a zero for a number no fold reported', async () => {
    const bundle = populatedBundle()
    // A retention cut can leave the opener out of the fold, so `entries` is
    // absent. A 0 there would read as a measurement nobody made.
    delete (bundle.status.value as Record<string, unknown>).entries
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Entries')).toBeInTheDocument())
    const row = screen.getByText('Entries').parentElement
    expect(row?.textContent).toContain('—')
    expect(row?.textContent).not.toContain('0')
  })

  it('says nothing is recorded when every fold is at seq 0', async () => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(emptyBundle()))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() =>
      expect(screen.getByText('Nothing recorded for this session')).toBeInTheDocument())
    expect(screen.getByText('up to date; no entries yet')).toBeInTheDocument()
    // No section headers: a fold with no entries has nothing to summarise, and a
    // row of five zeroes would assert a measurement of an empty file.
    expect(screen.queryByText('Status')).not.toBeInTheDocument()
  })

  it('re-reads when a turn ends, and not when one starts', async () => {
    const store = createTestStore()
    store.dispatch(setActiveSlot(SLOT))
    renderWithProviders(<CrewLogTab slot={SLOT} />, { store })
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(1))

    // A turn STARTS: the entries that matter have not been appended yet, so a
    // re-read here would spend a request to learn nothing.
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false }))
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(1))

    // The turn ENDS: fold again.
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: false, stopping: false }))
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(2))
  })

  it('re-reads when the turn ends even while a subagent is still running', async () => {
    // `selectComposerBusy` stays true while spawned work runs (chatSlice.ts:3923),
    // so watching only that signal meant a turn which finished alongside a
    // long-running subagent kept showing its PRE-turn fold for as long as the
    // subagent lived -- minutes, not a moment. The session's own turn edge has to
    // be watched as well.
    const store = createTestStore()
    store.dispatch(setActiveSlot(SLOT))
    renderWithProviders(<CrewLogTab slot={SLOT} />, { store })
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(1))

    store.dispatch(setSlotState('streaming'))
    // A subagent is spawned and is still pending when the turn itself finishes.
    store.dispatch(sseSubagentPending({ slot: SLOT, id: 'sub-1', task: 'a task', approval_id: 'ap-1' }))
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(1))

    // The TURN ends. The composer is still busy because the subagent is alive, so
    // this edge is the only thing that can fold the entries the turn just wrote.
    store.dispatch(setSlotState('idle'))
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(2))
  })

  it('renders an epoch-millisecond stamp as a clock time', async () => {
    // Every fold passes an entry's `time` through UNCHANGED, and the store stamps
    // it in epoch milliseconds — so a reader that only parses date strings shows a
    // dash next to data it was handed.
    const bundle = populatedBundle()
    ;(bundle.status.value as Record<string, unknown>).opened_at = 1789822000000
    bundle.timeline.value = {
      ...bundle.timeline.value,
      moments: [{ seq: 1, time: 1789822000000, type: 'session/opened' }],
    }
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Opened')).toBeInTheDocument())
    const row = screen.getByText('Opened').parentElement
    expect(row?.textContent).not.toContain('—')
    expect(row?.textContent).toMatch(/\d/)
    fireEvent.click(screen.getByText('Timeline'))
    const moment = screen.getByText('session opened').parentElement
    expect(moment?.textContent).toMatch(/\d/)
  })

  it('hands a failed turn error to the shared notice, not a coloured span', async () => {
    const bundle = populatedBundle()
    ;(bundle.status.value as Record<string, unknown>).last_error = 'ProviderTimeout'
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    // ErrorNotice renders its own alert region and offers the agent hand-off; a
    // hand-written span would show the text and lose both.
    await waitFor(() => expect(screen.getByText('ProviderTimeout')).toBeInTheDocument())
    expect(screen.getByText('ProviderTimeout').closest('[role="alert"]')).not.toBeNull()
  })

  it('drops the per-model table when one model would repeat the tile', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Usage')).toBeInTheDocument())
    // One model: the fixture's only row would restate the credits figure the
    // header summary and the tile already carry.
    expect(screen.queryByText('model')).not.toBeInTheDocument()

    const bundle = populatedBundle()
    bundle.usage.value = {
      ...bundle.usage.value,
      by_model: {
        'a-model': { turns: 8, credits: 6.0, credits_reported: 8, tokens: 900000 },
        'b-model': { turns: 4, credits: 2.41, credits_reported: 4, tokens: 304913 },
      },
    }
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getAllByText('model').length).toBeGreaterThan(0))
  })

  it('says a known stop reason in words and passes an unknown one through', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('finished its turn')).toBeInTheDocument())
    expect(screen.queryByText('end_turn')).not.toBeInTheDocument()

    const bundle = populatedBundle()
    ;(bundle.status.value as Record<string, unknown>).last_stop_reason = 'provider_reset'
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    // Unknown reasons are the provider's own vocabulary; showing the raw value
    // beats mapping it to a word nobody measured.
    await waitFor(() => expect(screen.getByText('provider_reset')).toBeInTheDocument())
  })

  it('names each recorded decision instead of printing its key', async () => {
    // The gateway records `approved`, `rejected` and `rejected_once`. A raw key
    // beside the translated Asked/Decided rows reads as a guess, and an
    // unrecognised value must still be shown rather than dropped.
    const bundle = populatedBundle()
    Object.assign((bundle.approvals.value as Record<string, unknown>), {
      by_decision: { approved: 2, rejected_once: 1, some_new_outcome: 1 },
    })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    // Approvals is one of the folds that starts collapsed, so its rows only exist
    // once the header is opened.
    await waitFor(() => expect(screen.getByText('Approvals')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Approvals'))
    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.getByText('Rejected once')).toBeInTheDocument()
    expect(screen.getByText('some_new_outcome')).toBeInTheDocument()
  })

  it('counts the rows it hides itself, not just the ones the fold dropped', async () => {
    // TWO caps stack on each of these lists: the fold's own retention budget,
    // which it reports, and this panel's 12-row draw. Reporting only the fold's
    // number is silent exactly when a list is longest -- 20 tools with a fold that
    // kept them all would have said nothing over a table showing 12 -- so the
    // reader is told the sum. A `*_saturated` fold counter is a FLOOR, so the line
    // has to say "at least" rather than print it as exact.
    const bundle = populatedBundle()
    const tools: Record<string, unknown> = {}
    for (let i = 0; i < 20; i++) {
      tools[`tool_${i}`] = { calls: 20 - i, completed: 20 - i, errors: 0, elapsed_ms: 100, last_status: 'ok', last_time: '', servers: [], servers_omitted: 0 }
    }
    Object.assign((bundle.tools.value as Record<string, unknown>), {
      by_name: tools,
      names_omitted: 5,
      names_omitted_saturated: true,
      open_calls: Array.from({ length: 14 }, (_, i) => ({ call_id: `c-${i}`, name: 'execute_bash', time: 1789822921000, seq: 1800 + i })),
      open_calls_omitted: 3,
      open_dropped: 2,
    })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Tools')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Tools'))
    // 20 names held, 12 drawn, 5 the fold never kept -- and the fold says its own
    // count is saturated, so the total is a lower bound.
    expect(screen.getByText('tools not detailed: at least 13')).toBeInTheDocument()
    // 14 listed by the fold, 12 drawn, 3 it held back, 2 it never retained.
    expect(screen.getByText('unfinished calls not listed: 7')).toBeInTheDocument()
  })

  it('defines a turn before it defines a refused one', async () => {
    // The hint hangs off the whole `Turns` label, whose value carries BOTH halves
    // ("completed: 12 · refused: 1"). A first version opened on the refusal, so a
    // reader hovering to learn what a turn is was told their 12 completed turns
    // never ran. The definition has to lead.
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(populatedBundle()))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Lifecycle')).toBeInTheDocument())
    const hint = screen.getByTitle(/worked on from start to finish/)
    expect(hint).toHaveTextContent('Turns')
    const text = hint.getAttribute('title') ?? ''
    expect(text.indexOf('start to finish')).toBeLessThan(text.indexOf('Refused'))
  })

  it('says a record is unaddressable rather than claiming nothing was recorded', async () => {
    // An idle reset tears the ACP session down and leaves the record on disk under
    // the retired id. Both cases arrive as an empty fold, so the panel used to tell
    // that reader "Nothing recorded for this session" -- false, about their own
    // data. The read reports which case it is.
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(emptyBundle(), { resolved: false }))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() =>
      expect(screen.getByText('No record addressable for this session')).toBeInTheDocument())
    expect(screen.queryByText('Nothing recorded for this session')).not.toBeInTheDocument()
    // Both causes named: a slot that has not run a turn, and a retired unit.
    expect(screen.getByText(/Run a turn to start a new record/)).toBeInTheDocument()
    expect(screen.getByText(/may not have run a turn yet/)).toBeInTheDocument()
  })

  it('says the fold may be behind when the writer had not drained', async () => {
    // The emitter queues an append and returns, so a fold taken as a turn ends can
    // miss what that turn wrote. "up to date through entry N" would then be the one
    // line in the footer that the record cannot back.
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(populatedBundle(), { writesDrained: false }))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText(/writes still pending/)).toBeInTheDocument())
  })

  it('dashes usage nothing measured instead of claiming zero', async () => {
    // A turn the gateway synthesizes for a failure reports no usage at all, so the
    // totals are 0 with 0 reporters. Rendering "0" claims the session was free and
    // took no time; the fold counts reporters separately precisely so a reader can
    // be told "unknown" instead.
    const bundle = populatedBundle()
    Object.assign((bundle.usage.value as Record<string, unknown>), {
      turns: { completed: 1, credits_reported: 0, tokens_reported: 0, duration_reported: 0 },
      credits: 0,
      tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0 },
      duration_ms: 0,
      by_model: {},
    })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Lifecycle')).toBeInTheDocument())
    // The collapsed header is the most-read copy of the claim, so it dashes too.
    expect(screen.getByText('credits: — · tokens: —')).toBeInTheDocument()
    // Tiles and the token breakdown: four rows plus three tiles, all unknown.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(7)
    // A compaction count really was measured, so it is not dashed.
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('shows a closed session its close time and reason', async () => {
    // `closed_at` is epoch MILLISECONDS, like every stamp a fold passes through, so
    // a presence test written as `str(v) && ...` drops the row for every closed
    // session -- the common path, not an edge case.
    const bundle = populatedBundle()
    Object.assign(bundle.status.value as Record<string, unknown>, {
      lifecycle: 'closed',
      closed_at: 1789822000000,
      close_reason: 'archived',
    })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Closed')).toBeInTheDocument())
    const row = screen.getByText('Closed').parentElement
    expect(row?.textContent).toMatch(/\d/)
    expect(row?.textContent).toContain('archived')
  })

  it('surfaces a read failure instead of rendering an empty panel', async () => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockRejectedValue(new Error('crew log read refused'))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText(/crew log read refused/)).toBeInTheDocument())
  })

  it('re-reads when the panel is reopened, not just while it stays open', async () => {
    // The side panel unmounts a tab's body when another tab is shown, so a turn
    // that runs while this view is away never reaches its turn-end refetch. A
    // remount that trusted the cache would show the fold from before that turn
    // for as long as the session lived.
    const first = renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(1))
    first.unmount()
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(2))
  })

  it('refetches on the manual control', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(1))
    const button = screen.getByRole('button', { name: /Refresh/ })
    await waitFor(() => expect(button).not.toBeDisabled())
    fireEvent.click(button)
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledTimes(2))
  })
})
