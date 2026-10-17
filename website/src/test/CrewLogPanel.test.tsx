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
function read(
  folds: unknown,
  over: {
    resolved?: boolean; writesDrained?: boolean; recording?: boolean; flagValue?: string
    flagRecognised?: boolean; envFile?: string
  } = {},
) {
  return {
    folds, resolved: true, writesDrained: true, recording: true, flagValue: '', flagRecognised: true,
    envFile: '~/.kiro/crew/.env',
    ...over,
  } as never
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
        credits_by_source: {
          turn: { credits: 8.41, reported: 12 },
          subagent: { credits: 0.0, reported: 0 },
          background: { credits: 0.0, reported: 0 },
        },
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
    subagents: {
      name: 'subagents',
      seq: 1842,
      value: {
        by_id: {
          'sub-1': {
            agent_id: 'sub-1', seq_spawned: 1200,
            agent: 'kirocrew-worker', model: 'a-model',
            outcome: 'completed', ms: 41000, credits: 1.75, reason: '',
          },
          'sub-2': {
            agent_id: 'sub-2', seq_spawned: 1400,
            agent: 'kirocrew-worker', model: 'a-model',
            outcome: 'stopped', ms: 900, credits: null, reason: 'kill failed: pid 8821 still alive',
          },
          'sub-3': {
            agent_id: 'sub-3', seq_spawned: 1830,
            agent: 'kirocrew-knowledge', model: 'a-model',
            outcome: null, ms: null, credits: null, reason: '',
          },
        },
        running: 1,
        running_exact: true,
        omitted: 0,
        totals: {
          spawned: 3, completed: 1, failed: 0, stopped: 1, unknown: 0,
          closed_unmatched: 0,
        },
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
    for (const title of ['Status', 'Usage', 'Timeline', 'Tools', 'Approvals', 'Subagents']) {
      expect(screen.getByText(title)).toBeInTheDocument()
    }
    // The collapsed list folds still say how much they hold.
    expect(screen.getByText('moments: 3')).toBeInTheDocument()
    expect(screen.getByText('calls: 96 · unfinished: 2')).toBeInTheDocument()
    expect(screen.getByText('asked: 4 · pending: 1')).toBeInTheDocument()
    expect(screen.getByText('dispatched: 3 · running: 1')).toBeInTheDocument()
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
      expect(screen.getByText('No messages saved for this chat yet')).toBeInTheDocument())
    expect(screen.getByText('up to date; no entries yet')).toBeInTheDocument()
    // No section headers: a fold with no entries has nothing to summarise, and a
    // row of five zeroes would assert a measurement of an empty file.
    expect(screen.queryByText('Status')).not.toBeInTheDocument()
    // Recording is on, so the switch-off instructions are not on screen.
    expect(screen.queryByText(/KIROCREW_CREW_LOG/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('crew-log-off')).not.toBeInTheDocument()
    expect(screen.queryByTestId('crew-log-footer-off')).not.toBeInTheDocument()
  })

  it('says the crew log is off and the chat is not saved when the gateway reports it', async () => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(
      read(emptyBundle(), { recording: false, flagValue: 'fasle', flagRecognised: false }))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('The crew log is off')).toBeInTheDocument())
    // The body opens with the consequence and quotes the value that switched it off.
    // The consequence is its own line; nothing is on screen, so it is every message.
    expect(screen.getByText('Messages in this chat are not being saved.')).toBeInTheDocument()
    // A value the gateway does not recognise is said to be one BEFORE it is quoted, so it
    // is not read as the app misspelling a setting.
    expect(screen.getByText(/^KIROCREW_CREW_LOG has a value that is not recognised, “fasle”/))
      .toBeInTheDocument()
    expect(screen.queryByText(/0, false, no, off/)).not.toBeInTheDocument()
    // A warning, not the neutral empty state.
    const notice = screen.getByTestId('crew-log-off')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice.className).toContain('bg-warn-subtle')
    expect(screen.getByText('The crew log is off').className).toContain('text-warn')
    expect(screen.queryByText('No messages saved for this chat yet')).not.toBeInTheDocument()
    // The docs path is a real link, not text that only looks like one.
    const link = screen.getByRole('link', { name: 'what the crew log stores' })
    expect(link).toHaveAttribute('href', expect.stringContaining('docs/reference/crew-log/README.md'))
    expect(link).toHaveAttribute('target', '_blank')
    // Nothing is written, so the footer makes no claim about a record.
    expect(screen.queryByText('up to date; no entries yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/this chat is writing now/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Refresh/ })).toBeInTheDocument()
    // The footer slot still says something: a lone Refresh button reads as broken.
    expect(screen.getByTestId('crew-log-footer-off')).toHaveTextContent('Crew log off')
  })

  it('falls back to naming the variable when the gateway sends no value', async () => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(
      read(emptyBundle(), { recording: false, envFile: '~/elsewhere/.env' }))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('The crew log is off')).toBeInTheDocument())
    // The body leads with the fix.
    expect(screen.getByText(/^Remove KIROCREW_CREW_LOG from ~\/elsewhere\/\.env \(or wherever/))
      .toBeInTheDocument()
    // The file named is the one the gateway reported, not a hardcoded home.
    expect(screen.queryByText(/~\/\.kiro\/crew\/\.env/)).not.toBeInTheDocument()
    expect(screen.queryByText(/“”/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'what the crew log stores' })).toBeInTheDocument()
  })

  it('says the crew log is off above entries an earlier run wrote, and keeps their footer', async () => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(
      read(populatedBundle(), { recording: false, flagValue: '0' }))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('The crew log is off')).toBeInTheDocument())
    const notice = screen.getByTestId('crew-log-off')
    expect(notice).toHaveTextContent(/Remove KIROCREW_CREW_LOG from .*restart the gateway\. It is set to “0”\./)
    expect(notice).not.toHaveTextContent(/not recognised/)
    // Entries are on screen, so only NEW messages go unsaved.
    expect(screen.getByText('New messages in this chat are not being saved. The ones below stay.'))
      .toBeInTheDocument()
    expect(screen.queryByText('Messages in this chat are not being saved.')).not.toBeInTheDocument()
    // The footer leads with the status and keeps the saved entries' watermark.
    expect(screen.getByTestId('crew-log-footer-off'))
      .toHaveTextContent('Crew log off · last saved entry 1,842')
    // The entries stay readable below the notice.
    expect(screen.getByText('Status')).toBeInTheDocument()
    expect(notice.compareDocumentPosition(screen.getByText('Status')) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    // Nothing is being written, so the scope note about the log "being written now" goes.
    expect(screen.queryByText(/this chat is writing now/)).not.toBeInTheDocument()
    expect(screen.queryByText('No messages saved for this chat yet')).not.toBeInTheDocument()
  })

  it('keeps the footer when recording is on', async () => {
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(emptyBundle()))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('up to date; no entries yet')).toBeInTheDocument())
    expect(screen.getByText(/this chat is writing now/)).toBeInTheDocument()
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
    // `selectComposerBusy` stays true while spawned work runs (selectors.ts in
    // store/chat), so watching only that signal meant a turn which finished
    // alongside a long-running subagent kept showing its PRE-turn fold for as long
    // as the subagent lived -- minutes, not a moment. The session's own turn edge
    // has to be watched as well.
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
      expect(screen.getByText('Nothing saved since this chat’s last reset')).toBeInTheDocument())
    expect(screen.queryByText('No messages saved for this chat yet')).not.toBeInTheDocument()
    // Both causes named: a slot that has not run a turn, and a retired unit.
    expect(screen.getByText(/Run a turn to start saving messages again/)).toBeInTheDocument()
    expect(screen.getByText(/has not run a turn yet/)).toBeInTheDocument()
    expect(screen.getByText(/Messages saved before the reset stay saved/)).toBeInTheDocument()
    // Unlike the empty state, it claims no watermark: there is no record to be current with.
    expect(screen.queryByText('up to date; no entries yet')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Refresh/ })).toBeInTheDocument()
    expect(screen.queryByText(/retired id/)).not.toBeInTheDocument()
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
      credits_by_source: {
        turn: { credits: 0.0, reported: 0 },
        subagent: { credits: 0.0, reported: 0 },
        background: { credits: 0.0, reported: 0 },
      },
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

  it('shows credits a subagent spent even when no turn reported a cost', async () => {
    // `credits` covers three spenders, but the gate counts TURN reporters only.
    // A session billed by a child call -- a synthesized turn closer, or spend
    // from a background call -- carries `turn.reported: 0` beside a non-zero
    // total, so a turn-scoped gate dashes a number the fold holds. The gate is
    // the session-wide reporter count, the sum across sources.
    const bundle = populatedBundle()
    Object.assign(bundle.usage.value as Record<string, unknown>, {
      turns: { completed: 1, credits_reported: 0, tokens_reported: 1, duration_reported: 1 },
      credits: 0.75,
      credits_by_source: {
        turn: { credits: 0.0, reported: 0 },
        subagent: { credits: 0.75, reported: 1 },
        background: { credits: 0.0, reported: 0 },
      },
    })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Lifecycle')).toBeInTheDocument())
    // The collapsed header is the most-read copy of the claim: it carries the
    // real total, not a dash.
    expect(screen.getByText('credits: 0.75 · tokens: 1.2M')).toBeInTheDocument()
    // The expanded tile reads the same number.
    const creditsLabel = screen.getByText(/^credits ·/)
    expect(creditsLabel.parentElement?.textContent).toContain('0.75')
    expect(creditsLabel.parentElement?.textContent).not.toContain('—')
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

  it('draws the subagents table', async () => {
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    const table = screen.getByText('subagent').closest('table')
    expect(table).toMatchSnapshot()
  })

  it('tells a reported charge, an unreported one and a running child apart', async () => {
    // THREE states, and the middle one is the one a panel gets wrong: a child that
    // reported no charge must not be drawn as having cost 0.
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    // Addressed by POSITION, which also pins the order: the fold renders rows in the
    // order the children were dispatched, and two of these share an agent name. Filtered
    // to the CHILD rows -- a child that did not finish also emits a full-width reason row,
    // which has one cell rather than five.
    const all = [...screen.getByText('subagent').closest('table')!.querySelectorAll('tbody tr')]
    const rows = all.filter(tr => tr.querySelectorAll('td').length === 5)
    expect(rows).toHaveLength(3)
    // sub-2 was stopped, so its reason row is the one extra.
    expect(all).toHaveLength(4)
    expect(all[2].textContent).toContain('kill failed: pid 8821 still alive')
    const text = (n: number) => rows[n].textContent ?? ''
    // sub-1 finished and reported 1.75.
    expect(text(0)).toContain('finished')
    expect(text(0)).toContain('1.75')
    // sub-2 was stopped and reported nothing: said so, not zeroed.
    expect(text(1)).toContain('stopped')
    expect(text(1)).toContain('not reported')
    expect(text(1)).not.toMatch(/\b0\b/)
    // sub-3 has not closed, so it has neither an outcome nor a duration to draw --
    // and "0.0s" would read as a child that finished instantly.
    expect(text(2)).toContain('running')
    // A dash, NOT "not said": its cost is not knowable yet rather than withheld, and the
    // same words for both collapsed two of the three states back into one.
    expect(text(2)).not.toContain('not reported')
    expect(text(2)).not.toContain('0.0s')
  })

  it('routes every reason through the error surface, whatever the outcome says', async () => {
    // The container follows the OUTCOME, not whether a reason exists. A run the owner
    // stopped did not fail, and dressing it in a red triangle plus "Ask the agent"
    // contradicted the neutral pill one line above it.
    const bundle = populatedBundle()
    const byId = (bundle.subagents.value as Record<string, unknown>).by_id as Record<string, Record<string, unknown>>
    byId['sub-1'].outcome = 'failed'
    byId['sub-1'].reason = 'the child crashed'
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    // BOTH reach the hand-off, because both values came from an error field.
    const failedRow = screen.getByText('the child crashed').closest('tr')
    expect(failedRow?.querySelector('[role="alert"]')).not.toBeNull()
    const stoppedRow = screen.getByText('kill failed: pid 8821 still alive').closest('tr')
    expect(stoppedRow?.querySelector('[role="alert"]')).not.toBeNull()
    // And the outcome still owns the PILL, which is the distinction it is actually for.
    expect(screen.getByText('stopped')).toBeInTheDocument()
  })

  it('says the list cannot show children the fold never kept', async () => {
    const bundle = populatedBundle()
    const value = bundle.subagents.value as Record<string, unknown>
    // A session past the retention cap: `spawned` exceeds the rows by `omitted`, on
    // purpose, and a closer landed with no row to put it on.
    Object.assign(value, { omitted: 11, running: 12 })
    Object.assign(value.totals as object, { spawned: 14 })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    // Every unlisted child here is one the FOLD dropped, so the line closes rather than
    // pointing anywhere: a reader who counted 11 and went looking found nothing, which is
    // the dead end this wording exists to remove.
    // All three reconciliation sentences now open with the SAME clause, so the opener cannot
    // tell them apart -- the tail does. And the `gone` tail is a PREFIX of the `dropped`
    // one, so this discriminates on what `gone` must NOT say instead: it offers nothing to
    // ask for, because there is nothing left to ask for.
    const line = screen.getByText(/11 more counted above/)
    expect(line.textContent).toContain(
      '11 more counted above, not listed here. The record kept no row for them.',
    )
    expect(line.textContent).toContain('11 of them are still running.')
    // "ask the agent for", not the bare phrase: the bare one is the `AskAgentButton` label
    // and appears legitimately elsewhere on this panel.
    expect(line.textContent!.toLowerCase()).not.toContain('ask the agent for')
    // And the header reports every dispatch, not the row count.
    expect(screen.getByText('dispatched: 14 · running: 12')).toBeInTheDocument()
  })

  it('points at the agent for children this table trimmed, and names the rest separately', async () => {
    // The OTHER half of the split. A row past `TABLE_ROWS` is still in the projection the
    // panel already fetched, so it is reachable -- reporting it with the same sentence as
    // a dropped dispatch would either strand the reader or over-promise.
    const bundle = populatedBundle()
    const value = bundle.subagents.value as Record<string, unknown>
    const byId = value.by_id as Record<string, Record<string, unknown>>
    for (let i = 4; i <= 17; i += 1) {
      byId[`sub-${i}`] = {
        agent_id: `sub-${i}`, seq_spawned: 1830 + i,
        agent: 'kirocrew-worker', model: 'a-model',
        outcome: 'completed', ms: 1000, credits: 0.5, reason: '',
      }
    }
    Object.assign(value, { omitted: 2, running: 1 })
    Object.assign(value.totals as object, { spawned: 19, completed: 15 })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    // 17 children held, 12 drawn, 2 never retained.
    const line = screen.getByText(/7 more counted above/)
    // ONE sentence for the mixed case: a promised "full list" cannot include children the
    // record dropped, and "these" had nothing a reader could bind it to.
    expect(line.textContent).toContain(
      '7 more counted above, not listed here. Ask the agent for 5 of them. '
      + 'The record kept no row for the other 2.',
    )
    expect(line.textContent).not.toContain('see the full list')
    // A routine retention cap read as an incident to the reader UX sat with. The sentence
    // states what the record keeps, not that something was lost beyond recovery.
    expect(line.textContent).not.toContain('cannot be recovered')
  })

  it('orders rows by the dispatch seq, not by how an agent id happens to be spelled', async () => {
    // JSON revives an integer-like key ahead of every other key regardless of where it sat
    // in the document, so a runtime that spells an agent id as digits would silently
    // reorder a table that trusted object-key order.
    const bundle = populatedBundle()
    const value = bundle.subagents.value as Record<string, unknown>
    const byId = value.by_id as Record<string, Record<string, unknown>>
    byId['900'] = {
      agent_id: '900', seq_spawned: 1900,
      agent: 'last-dispatched', model: 'a-model',
      outcome: 'completed', ms: 1000, credits: 0.5, reason: '',
    }
    Object.assign(value.totals as object, { spawned: 4, completed: 2 })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(JSON.parse(JSON.stringify(bundle))))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    const all = [...screen.getByText('subagent').closest('table')!.querySelectorAll('tbody tr')]
    const rows = all.filter(tr => tr.querySelectorAll('td').length === 5)
    // Highest seq, so LAST -- even though its key sorts first across the JSON boundary.
    expect(rows[rows.length - 1].textContent).toContain('last-dispatched')
  })

  it('draws its own copy, not an empty table, for a session that dispatched nobody', async () => {
    // The state most sessions show on first expand.
    const bundle = populatedBundle()
    bundle.subagents.value = {
      by_id: {}, running: 0, running_exact: true, omitted: 0,
      totals: {
        spawned: 0, completed: 0, failed: 0, stopped: 0, unknown: 0,
        closed_unmatched: 0,
      },
    }
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    expect(screen.getByText('No subagents dispatched.')).toBeInTheDocument()
    // No header row over an empty body, which reads as a list that failed to load.
    expect(screen.queryByText('subagent')).toBeNull()
    expect(screen.queryByText('subagents dispatched')).toBeNull()
  })

  it('says "at least" wherever the fold reports its running count as a floor', async () => {
    // Both truncations at once: a dispatch dropped past the cap AND a closer that matched no
    // row. The fold subtracts that closer without being able to tell whether it belongs to
    // the dropped dispatch, so the count can be short -- and drawing it as exact states a
    // measurement the record cannot support.
    const bundle = populatedBundle()
    const value = bundle.subagents.value as Record<string, unknown>
    Object.assign(value, { omitted: 11, running: 12, running_exact: false })
    Object.assign(value.totals as object, { spawned: 14, closed_unmatched: 2 })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    // The collapsed header, which is where a reader meets the number first.
    expect(screen.getByText('dispatched: 14 \u00b7 running: at least 12')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Subagents'))
    expect(screen.getByText(/11 more counted above/).textContent)
      .toContain('At least 11 of them are still running.')
  })

  it('states the running count plainly when the fold reports it as exact', async () => {
    // One truncation is not enough: with nothing omitted, every open child has a visible row,
    // so the count cannot be short and hedging it would understate what the record knows.
    const bundle = populatedBundle()
    const value = bundle.subagents.value as Record<string, unknown>
    Object.assign(value, { omitted: 11, running: 12, running_exact: true })
    Object.assign(value.totals as object, { spawned: 14 })
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    expect(screen.getByText('dispatched: 14 \u00b7 running: 12')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Subagents'))
    expect(screen.getByText(/11 more counted above/).textContent)
      .toContain('11 of them are still running.')
  })

  it('keeps a truncated agent and model name readable', async () => {
    // Both cells clip at a `max-w`, and these names differ in their TAILS, so an ellipsis
    // can hide the only part that identifies the row.
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    const all = [...screen.getByText('subagent').closest('table')!.querySelectorAll('tbody tr')]
    const cells = all.filter(tr => tr.querySelectorAll('td').length === 5)[2].querySelectorAll('td')
    expect(cells[0].getAttribute('title')).toBe('kirocrew-knowledge')
    expect(cells[1].getAttribute('title')).toBe('a-model')
  })

  it('does not claim nothing was dispatched when only closers reached the log', async () => {
    // `spawned` at 0 is not "nothing happened": a child whose `subagent/spawned` append was
    // abandoned in a `write/dropped` marker still gets closed, so the fold bills
    // `closed_unmatched`, `ms` and `credits` against a child that genuinely ran.
    const bundle = populatedBundle()
    bundle.subagents.value = {
      by_id: {}, running: 0, running_exact: true, omitted: 0, limit: 512,
      totals: {
        spawned: 0, completed: 0, failed: 1, stopped: 0, unknown: 0,
        closed_unmatched: 1,
      },
    }
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    expect(screen.queryByText('No subagents dispatched.')).toBeNull()
    // And no `0` tile. A count of 0 an inch above "1 subagent finished" is a contradiction
    // on its face; the true-empty state already hides the tile and this state needs the
    // same treatment, so the sentence stands alone and says why the count would be 0.
    expect(screen.queryByText('subagents dispatched')).toBeNull()
    // And the header stays quiet. Both its counts would read 0 -- truthfully, since nothing
    // was recorded as dispatched and nothing is running -- an inch above a sentence saying
    // one child finished, which is the same contradiction the tile made one line down.
    expect(screen.queryByText(/dispatched: 0/)).toBeNull()
    // And it draws no table either. Declining the empty state leaves `by_id` empty, so the
    // child table would be a `thead` over an empty `tbody` -- the same "list that failed to
    // load" shape the sibling empty-state test forbids, reached by the other route. The
    // table is gated on having a row rather than on which escape fired above it.
    expect(screen.queryByText('subagent')).toBeNull()
    expect(screen.queryByText('outcome')).toBeNull()
    // What it says INSTEAD. A `0` tile alone leaves the reader with a cost the totals hold
    // and nothing naming it, and the SINGULAR form is the one this state produces most
    // often -- one lost `subagent/spawned` append is one child.
    expect(screen.getByText(
      '1 subagent finished with no start in the record, so it has no row here. '
      + 'Its credits are in this session\u2019s total.',
    )).toBeInTheDocument()
  })

  it('pluralises the unmatched-closer sentence rather than formatting its count', async () => {
    // Two closers, so the `other` form. The count goes through i18next as a raw number: a
    // pre-formatted string would pick the plural category for a STRING, which is `other`
    // whatever the number is, and the singular above would never render.
    const bundle = populatedBundle()
    bundle.subagents.value = {
      by_id: {}, running: 0, running_exact: true, omitted: 0,
      totals: {
        spawned: 0, completed: 1, failed: 1, stopped: 0, unknown: 0,
        closed_unmatched: 2,
      },
    }
    vi.spyOn(api, 'sessionCrewLogProjections').mockResolvedValue(read(bundle))
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    expect(screen.getByText(
      '2 subagents finished with no start in the record, so they have no rows here. '
      + 'Their credits are in this session\u2019s total.',
    )).toBeInTheDocument()
  })

  it('states on the credits column whether a child charge is inside the session total', async () => {
    // Two money figures on one panel with no stated relationship is what a reader hit:
    // they could not tell whether the children's cost was already in Usage's credits.
    renderWithProviders(<CrewLogTab slot={SLOT} />)
    await waitFor(() => expect(screen.getByText('Subagents')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Subagents'))
    const header = screen.getByText('subagent').closest('table')!
      .querySelector('thead tr th:last-child')
    expect(header?.textContent).toBe('credits')
    expect(header?.getAttribute('title'))
      .toBe('Already counted in this session’s credits total, not a charge on top of it. '
        + 'A child that failed or was stopped still bills for what it used.')
  })
})
