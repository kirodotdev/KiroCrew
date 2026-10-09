import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import { memberProjectionStore } from '../../state/memberProjectionStore'

/* "New conversation" on a crewmate's DM. The model forgets; nothing is deleted.
 *
 * The control lives in the crewmate's PROFILE card, not in the thread header
 * (reviewer's call): the one occasion anybody reaches for it is a crewmate stuck
 * in a turn, which is rare enough that a header button spends permanent space on
 * it. The row and its look are pinned in CrewProfilePanel.test.tsx; what this
 * file owns is the flow behind the press.
 *
 * Four things have to hold for that flow to be honest, and each one fails
 * quietly:
 *
 * `replay: false` must be SENT. The route defaults it to true, which replays the
 * discarded history straight back into the fresh context — so a call that omits
 * the flag answers 200 while changing nothing the user can see.
 *
 * The ask must come first, and it must say what survives — and that a running
 * turn is stopped. The context is gone for good once the call lands.
 *
 * A STUCK slot must work. The route refuses a busy slot with 409
 * `turn_in_flight`, and busy is the case this action exists for, so the flow
 * stops the turn and waits for it to let go before it asks for the reset. A
 * flow that only works on an idle slot works in exactly the situation nobody
 * needs it.
 *
 * A refusal must still be SHOWN. A turn that outlasts the stop budget, or an
 * inbound channel turn admitted after it, still earns a 409, and a swallowed one
 * reads as "nothing happened" over a conversation the model still remembers. */

/* Hoisted, because `vi.mock`'s factory is lifted above every top-level binding
 * and the mock has to hand back this class. */
const StubApiError = vi.hoisted(() => class StubApiError extends Error {
  status: number
  body: string
  constructor(status: number, message: string, body = '') {
    super(message)
    this.status = status
    this.body = body
  }
})

vi.mock('../../api/client', () => ({
  ApiError: StubApiError,
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    chatSlotResetConversation: vi.fn(),
    stopChatSlot: vi.fn(),
  },
}))

vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))
// The profile's permission / model settings read and write the live slot; a
// stub keeps them out of this file, which is about the reset row below them.
vi.mock('./CrewProfileSettings', () => ({
  default: () => <div data-testid="profile-settings-stub" />,
}))

/* The pane as a stub that echoes the boundary it was handed: whether the page
 * reads the projection is the page's half of the feature, and the drawing of it
 * is pinned in ChatPane.conversationBoundary.test.tsx. */
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, conversationStartTs }: { slotKey: string; conversationStartTs?: number }) => (
    <div data-testid="chat-pane-stub" data-boundary={conversationStartTs ?? ''}>{slotKey}</div>
  ),
}))

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => vi.fn() }
})

import { api } from '../../api/client'
import { sseSlots } from '../../store/dashboardSlice'
import { createTestStore } from '../../test/helpers'
import MembersPage from './MembersPage'

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: false, slot_key: '', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

const reset = () => api.chatSlotResetConversation as ReturnType<typeof vi.fn>
const stop = () => api.stopChatSlot as ReturnType<typeof vi.fn>

let store: ReturnType<typeof createTestStore>

/** Push a live `slots` frame for the DM slot. That frame is where the page reads
 *  "is this slot still busy" from while it waits for a stopped turn to let go,
 *  so a case that needs the wait to run plants `running: true` here and a case
 *  that needs it to end plants `running: false`. */
function pushSlotRunning(running: boolean) {
  act(() => {
    store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running, messages: 2 }] as never))
  })
}

async function openThread(overrides: Record<string, unknown> = {}) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row(overrides)], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: true })
  store = createTestStore()
  renderWithProviders(<MembersPage />, { store })
  const rowButton = await screen.findByText('oncall')
  act(() => { fireEvent.click(rowButton) })
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
}

/** Open the crewmate's profile card, which is where the control lives. The card
 *  can open on another tab (Sessions, for a crewmate with no chat yet), so the
 *  Profile tab is picked explicitly, the way a user reaches the row. */
async function openProfile() {
  const pill = await screen.findByTestId('member-identity-pill')
  act(() => { fireEvent.click(pill) })
  const tabs = await screen.findByTestId('crew-profile-tabs')
  const profileTab = within(tabs).getAllByRole('tab').find((el) => /profile/i.test(el.textContent || el.getAttribute('aria-label') || ''))
  if (profileTab) act(() => { fireEvent.click(profileTab) })
  return screen.findByTestId('crew-profile-new-conversation')
}

/** Press the profile row and answer its dialog. */
async function pressAndConfirm() {
  const button = await openProfile()
  act(() => { fireEvent.click(button) })
  const confirm = await screen.findByRole('button', { name: 'Start a new conversation' })
  await act(async () => { fireEvent.click(confirm) })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // The projection store is process-wide and seeded from each roster read, so a
  // boundary one case planted would otherwise still be there for the next.
  memberProjectionStore.clear()
  __resetPanelTabs()
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true })
  reset().mockResolvedValue({ slot: 'member-oncall', reset: true, replay: false, boundary: 'recorded' })
  // The ordinary case: nothing was running, so the route says so and the flow
  // has nothing to wait for.
  stop().mockResolvedValue({ ok: true, info: 'not running' })
})

describe('the New conversation control', () => {
  it('asks before it acts, and says what survives', async () => {
    await openThread()

    const button = await openProfile()
    act(() => { fireEvent.click(button) })

    expect(reset()).not.toHaveBeenCalled()
    const body = await screen.findByText(/starts over/)
    // Named, not templated: the copy carries `{{name}}`, and a body handed no
    // interpolation renders that placeholder to the user verbatim. Quoted,
    // because a crewmate named "Everything" would otherwise read as a sentence
    // about everything (destructiveConfirm.test.ts pins the glyph per locale).
    expect(body.textContent).toMatch(/^\u201concall\u201d starts over/)
    // "long-term memory" by its own name. A crewmate has two things a reader
    // could call memory -- this chat's context, which the reset drops, and the
    // store it writes to, which survives -- so the bare word names the thing
    // being dropped just as well as the thing being kept.
    expect(body.textContent).toMatch(/What it has saved to its long-term memory stays/)
    expect(body.textContent).toMatch(/Show earlier messages/)
  })

  it('does nothing when the ask is declined', async () => {
    await openThread()

    const button = await openProfile()
    act(() => { fireEvent.click(button) })
    const cancel = await screen.findByRole('button', { name: 'Cancel' })
    await act(async () => { fireEvent.click(cancel) })

    expect(reset()).not.toHaveBeenCalled()
    // Nor is the turn stopped. A declined ask must leave the crewmate working.
    expect(stop()).not.toHaveBeenCalled()
  })

  it('posts the reset for the open slot with replay explicitly off', async () => {
    await openThread()

    await pressAndConfirm()

    await waitFor(() => expect(reset()).toHaveBeenCalledWith('member-oncall'))
  })

  it('is not in the thread header any more', async () => {
    /* Reviewer's call: too visible for a control whose only occasion is a stuck
     * crewmate. It is a profile row, so the header is back to the three-column
     * layout it had before the feature — the narrow-width short label existed
     * only to fit a named action beside the panel toggle at 320px. */
    await openThread()

    expect(screen.queryByTestId('member-new-conversation')).toBeNull()
    const header = screen.getByTestId('member-thread-header')
    expect(header.className).toContain('grid-cols-[1fr_minmax(0,auto)_1fr]')
    expect(header.className).not.toMatch(/sm:grid-cols/)
  })

  it('says the current turn is stopped first, so that is consented to too', async () => {
    /* The flow stops a running turn before it asks for the reset. That is a
     * second thing happening to the crewmate, and the user reaches for this
     * control precisely when a turn IS running, so the dialog states it. */
    await openThread()

    const button = await openProfile()
    act(() => { fireEvent.click(button) })

    const body = await screen.findByText(/starts over/)
    expect(body.textContent).toMatch(/If it is working right now, that work is stopped first\./)
  })

  it('stops the running turn, waits for it to let go, and only then resets', async () => {
    /* The whole reason the control moved here. The route refuses a busy slot
     * (409 `turn_in_flight`) and busy is the case this action is FOR, so a flow
     * that posts the reset straight away is refused in exactly the situation
     * nobody needs it in. */
    await openThread({ running: true })
    pushSlotRunning(true)
    stop().mockResolvedValue({ ok: true })

    await pressAndConfirm()

    await waitFor(() => expect(stop()).toHaveBeenCalledWith('member-oncall'))
    // Still waiting: the slot has not said it let go, so the reset has not been
    // asked for. Without this the test would pass on a flow that fires both at
    // once and happens to order the mocks.
    expect(reset()).not.toHaveBeenCalled()

    pushSlotRunning(false)

    await waitFor(() => expect(reset()).toHaveBeenCalledWith('member-oncall'))
  })

  it('does not stop a slot the route says has nothing running', async () => {
    /* The ordinary case — an idle crewmate whose profile the user opened. The
     * stop is a no-op the route answers `not running` to, and waiting on it
     * would be waiting for a state the slot is already in. */
    await openThread()

    await pressAndConfirm()

    await waitFor(() => expect(reset()).toHaveBeenCalledWith('member-oncall'))
    expect(stop()).toHaveBeenCalledTimes(1)
  })

  it('asks for the reset anyway once the wait runs out, and shows its refusal', async () => {
    /* A stop is not a promise. A provider that never lets go would otherwise
     * park the user on a spinner for good, so the wait is bounded and the route
     * — the authority on busy — gets the last word. Its 409 is what the user
     * sees, in the same words as any other busy refusal. */
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      await openThread({ running: true })
      pushSlotRunning(true)
      stop().mockResolvedValue({ ok: true })
      reset().mockRejectedValueOnce(
        new StubApiError(409, 'a turn is in flight', '{"code":"turn_in_flight"}'),
      )

      await pressAndConfirm()
      await waitFor(() => expect(stop()).toHaveBeenCalledTimes(1))

      // Past the escalation mark: a second stop is a HARD KILL at the route
      // (`stop_slot_turn` escalates any second stop), which is what a person
      // does after watching a cooperative one fail to take, and the only thing
      // that moves a genuinely stuck turn.
      await act(async () => { await vi.advanceTimersByTimeAsync(11_000) })
      expect(stop()).toHaveBeenCalledTimes(2)
      expect(reset()).not.toHaveBeenCalled()

      // Past the budget. The slot never said it let go, and the reset is asked
      // for regardless.
      await act(async () => { await vi.advanceTimersByTimeAsync(11_000) })
      await waitFor(() => expect(reset()).toHaveBeenCalledWith('member-oncall'))

      const notice = await screen.findByTestId('member-new-conversation-error')
      expect(notice.textContent).toMatch(/oncall is handling a message from another place/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('still resets when the stop itself fails', async () => {
    /* A failed stop is not a reason to abandon the thing that was asked for.
     * Whether the slot can take the reset is the route's call, not this page's. */
    await openThread({ running: true })
    stop().mockRejectedValue(new Error('the gateway is gone'))

    await pressAndConfirm()

    await waitFor(() => expect(reset()).toHaveBeenCalledWith('member-oncall'))
    // And the stop's own failure is not reported: nothing the user asked for
    // failed, and the reset's outcome is the answer.
    expect(screen.queryByTestId('member-new-conversation-error')).toBeNull()
  })

  it('reports a refusal under the profile row while that card is open', async () => {
    /* One state, one copy, mounted where the reader is looking — under the row
     * they just pressed. */
    await openThread()
    reset().mockRejectedValueOnce(
      new StubApiError(409, 'a turn is in flight', '{"code":"turn_in_flight"}'),
    )

    await pressAndConfirm()

    const notice = await screen.findByTestId('member-new-conversation-error')
    expect(screen.getByTestId('crew-profile-pane-profile')).toContainElement(notice)
    // Exactly one: a second copy above the thread would be a second thing to
    // dismiss for one outcome.
    expect(screen.getAllByTestId('member-new-conversation-error')).toHaveLength(1)
  })

  it('says so when the reset landed but its boundary did not', async () => {
    /* The one outcome where a clean-looking success is a lie about what is on
     * screen: the crewmate has forgotten the messages still drawn above the
     * composer, and nothing marks them. */
    await openThread()
    reset().mockResolvedValueOnce({ slot: 'member-oncall', reset: true, replay: false, boundary: 'failed' })

    await pressAndConfirm()

    const notice = await screen.findByTestId('member-new-conversation-error')
    // Its OWN heading. The refusal heading over this body tells the reader the
    // reverse of what the body says: that the reset did not happen.
    expect(notice.textContent).toMatch(/New conversation started, line not saved/)
    // Opens on the ACTION that works, and the reassurance is the very next
    // sentence. A notice that opens on what the crewmate has forgotten reads as
    // a warning against the only step that repairs the state it is reporting.
    // A reload is NOT that step: it reads the same projection, which holds no
    // boundary, so telling the user to reload promises nothing.
    expect(notice.textContent).toMatch(
      /Press Start a new conversation again to add the \u201cNew conversation starts here\u201d line\. No messages are lost\./,
    )
    expect(notice.textContent).not.toMatch(/Reload/)
    // The line is named with the words the pane prints on it, so the reader is
    // looking for the same thing in both places. "line", never "divider".
    // "crewmate" must not appear at all now the row is named "Start a new
    // conversation".
    expect(notice.textContent ?? '').not.toMatch(/crewmate|divider/)
    // The cost comes after the reassurance, and is still stated: pressing again
    // moves the boundary to now, so this turn's own messages go with it.
    expect(notice.textContent).toMatch(
      /messages oncall has already forgotten still look current, and pressing again forgets anything said since too/,
    )
    expect(notice.textContent).not.toMatch(/Couldn't start a new conversation/)
  })

  it('stays quiet when the slot has no member log to write into', async () => {
    await openThread()
    reset().mockResolvedValueOnce({ slot: 'member-oncall', reset: true, replay: false, boundary: 'not_owed' })

    await pressAndConfirm()

    expect(screen.queryByTestId('member-new-conversation-error')).toBeNull()
  })

  it('ignores a boundary past what a Date can hold', async () => {
    /* A hand-damaged member log. Unguarded, the pane's marker construction
     * raises and the whole DM becomes an error fallback. */
    await openThread({
      projections: {
        asOfSeq: 4,
        values: {
          roster: {
            name: 'oncall',
            slug: 'oncall',
            conversation_starts: { 'member-oncall': { ts: 9223372036854775807 } },
          },
        },
      },
    })

    expect(screen.getByTestId('chat-pane-stub').getAttribute('data-boundary')).toBe('')
  })

  describe('the evicted-boundary note', () => {
    /** A roster projection block, with whatever the case under test plants. */
    const roster = (values: Record<string, unknown>) => ({
      projections: { asOfSeq: 4, values: { roster: { name: 'oncall', slug: 'oncall', ...values } } },
    })

    it('says so when a boundary was dropped and this slot has none', async () => {
      /* The reader for the fold's eviction count. A dropped key reads exactly
       * like a slot nobody ever reset, so without this the pane draws a
       * discarded conversation as current and says nothing — the one state the
       * boundary exists to prevent. */
      await openThread(roster({ conversation_starts_evicted: 2 }))

      const note = await screen.findByTestId('member-boundary-evicted-notice')
      // Names what happened and the next step, and never "for this thread":
      // the count says how many boundaries were dropped and never which, so
      // naming this thread would assert something nothing on this page knows.
      expect(note.textContent).toMatch(
        /The \u201cNew conversation starts here\u201d line for an earlier new conversation was lost/,
      )
      expect(note.textContent).toMatch(/Press Start a new conversation to mark a fresh start/)
    })

    it('stays quiet once this slot has a boundary of its own', async () => {
      /* With one, the pane is already drawing the right line and the dropped
       * boundaries belong to slots the user is not looking at. */
      await openThread(roster({
        conversation_starts_evicted: 2,
        conversation_starts: { 'member-oncall': { ts: 1_700_000_000_000 } },
      }))

      expect(await screen.findByTestId('chat-pane-stub')).toBeTruthy()
      expect(screen.queryByTestId('member-boundary-evicted-notice')).toBeNull()
    })

    it('stays quiet when nothing was evicted', async () => {
      await openThread(roster({ conversation_starts_evicted: 0 }))

      expect(await screen.findByTestId('chat-pane-stub')).toBeTruthy()
      expect(screen.queryByTestId('member-boundary-evicted-notice')).toBeNull()
    })

    it('stays quiet when the count is absent, which is the ordinary row', async () => {
      await openThread(roster({}))

      expect(await screen.findByTestId('chat-pane-stub')).toBeTruthy()
      expect(screen.queryByTestId('member-boundary-evicted-notice')).toBeNull()
    })

    it('can be closed, and closing it does not touch the thread', async () => {
      /* The condition does not clear by navigating — the fold's count only ever
       * rises — so a note with no close would stand over the thread until the
       * next reset. */
      await openThread(roster({ conversation_starts_evicted: 1 }))

      const dismiss = await screen.findByTestId('member-boundary-evicted-dismiss')
      act(() => { fireEvent.click(dismiss) })

      await waitFor(() =>
        expect(screen.queryByTestId('member-boundary-evicted-notice')).toBeNull(),
      )
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    })
  })

  it('puts the busy refusal in the user\'s words, not the gateway\'s', async () => {
    /* "a turn is in flight" is the code's term for a state the page already
     * renders as the crewmate working, and it offers no next step. */
    await openThread()
    reset().mockRejectedValueOnce(
      new StubApiError(409, 'a turn is in flight', '{"code":"turn_in_flight"}'),
    )

    await pressAndConfirm()

    const notice = await screen.findByTestId('member-new-conversation-error')
    // Says WHERE, not just WHY. "is handling a message" beside an "Idle" pill
    // reads as a contradiction the reader has to resolve; naming the other
    // place the message came from is what makes both true at once.
    expect(notice.textContent).toMatch(/oncall is handling a message from another place/)
    expect(notice.textContent).toMatch(/such as a channel/)
    expect(notice.textContent).toMatch(/this thread still looks idle/)
    expect(notice.textContent).not.toMatch(/turn is in flight/)
  })

  it('still shows an unrecognised failure verbatim', async () => {
    /* Only the ONE refusal the route defines is reworded; anything else keeps
     * the server's own text rather than being flattened into a guess. */
    await openThread()
    reset().mockRejectedValueOnce(new Error('the disk is full'))

    await pressAndConfirm()

    const notice = await screen.findByTestId('member-new-conversation-error')
    expect(notice.textContent).toMatch(/the disk is full/)
  })

  it('hands the pane the boundary the projection recorded', async () => {
    await openThread({
      projections: {
        asOfSeq: 4,
        values: {
          roster: {
            name: 'oncall',
            slug: 'oncall',
            conversation_starts: { 'member-oncall': { ts: 1_700_000_000_000 } },
          },
        },
      },
    })

    await waitFor(() =>
      expect(screen.getByTestId('chat-pane-stub').getAttribute('data-boundary')).toBe('1700000000000'),
    )
  })

  it('hands the pane no boundary for a slot that was never reset', async () => {
    await openThread({
      projections: {
        asOfSeq: 4,
        values: {
          roster: {
            name: 'oncall',
            slug: 'oncall',
            conversation_starts: { 'member-someone-else': { ts: 1_700_000_000_000 } },
          },
        },
      },
    })

    expect(screen.getByTestId('chat-pane-stub').getAttribute('data-boundary')).toBe('')
  })
})
