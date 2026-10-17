import { useCallback, useEffect, useState } from 'react'
import type { Dispatch, MutableRefObject, RefObject, SetStateAction } from 'react'
import type { QueryClient } from '@tanstack/react-query'

import { commandFolderName, fileSessionInCommandFolder } from './sessionFolder'
import type { ChatFolderRow } from './sessionFolder'
import type { AppDispatch } from '../../store'
import { createSlot, setPendingInput, switchSlot } from '../../store/chatSlice'
import { i18nT } from '../../i18n/t'

import { recordUse, type UsageMap } from './frecency'
import type { RankedRow } from './rootIndex'
import type { ContributedCommand } from './contributedCommands'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import { ASK_SLOT_KEY, type Scope } from './commandBarOverlayModel'
import type { useCommandBarOverlaySlots } from './useCommandBarOverlaySlots'

/**
 * What Enter and a click do with a slot: entering a scope, running a root row,
 * seeding a new session for the ask row and the contributed commands, and the latch
 * that holds a keyboard Enter pressed before a scoped view's rows caught up with the
 * query. A new slot tag's activation belongs here.
 */
export function useCommandBarOverlayActivation({
  open,
  onClose,
  scope,
  setScope,
  query,
  setQuery,
  setDebounced,
  selected,
  setSelected,
  inputRef,
  setUsage,
  setArgCommand,
  pendingRow,
  setPendingRow,
  setActionError,
  dialogRunRef,
  commandById,
  commandByIdRef,
  chatFoldersRef,
  dispatch,
  navigate,
  queryClient,
  slots,
  rowsAnswerTheQuery,
  scopeLoading,
  scopeFetching,
  refetchSessions,
  refetchArtifacts,
  refetchFolders,
  refetchMates,
}: Pick<
  ReturnType<typeof useCommandBarOverlaySlots>,
  | 'slots'
  | 'rowsAnswerTheQuery'
  | 'scopeLoading'
  | 'scopeFetching'
  | 'refetchSessions'
  | 'refetchArtifacts'
  | 'refetchFolders'
  | 'refetchMates'
> & {
  open: boolean
  onClose: () => void
  scope: Scope
  setScope: Dispatch<SetStateAction<Scope>>
  query: string
  setQuery: Dispatch<SetStateAction<string>>
  setDebounced: Dispatch<SetStateAction<string>>
  selected: number
  setSelected: Dispatch<SetStateAction<number>>
  inputRef: RefObject<HTMLInputElement | null>
  setUsage: Dispatch<SetStateAction<UsageMap>>
  setArgCommand: Dispatch<SetStateAction<ContributedCommand | null>>
  pendingRow: string | null
  setPendingRow: Dispatch<SetStateAction<string | null>>
  setActionError: Dispatch<SetStateAction<string | null>>
  dialogRunRef: MutableRefObject<number>
  commandById: Map<string, ContributedCommand>
  commandByIdRef: MutableRefObject<Map<string, ContributedCommand>>
  chatFoldersRef: MutableRefObject<unknown>
  dispatch: AppDispatch
  navigate: (route: string) => void
  queryClient: QueryClient
}) {
  /**
   * A keyboard Enter that landed in the debounce window, held until the rows answer
   * the query it was pressed against -- and no later one.
   *
   * A scoped view ranks from the DEBOUNCED query, so for one interval after a
   * keystroke its rows answer the previous one. An Enter then names a row built from
   * the old query, and this latch is the launcher's primary gesture -- type a name,
   * confirm it -- waiting for the rows to catch up rather than being eaten. The latch
   * remembers the exact query the reader confirmed and the row they had selected: it
   * fires once, on the live rows, only when the live query is still that query, the
   * selection is still that row, and the top of the list is a settled result. Any
   * further input, any selection move, a pointer activation, Escape, leaving the
   * scope, or closing the bar drops it instead -- a latch that outlived the reader's
   * confirmation would open a query they never confirmed, or a row they moved off of.
   * A read that fails with no cached rows leaves a retry row at the top, which is not
   * a result, so the latch drops on that dead end; a refetch that fails while the
   * confirmed query's rows are still cached fires those rows -- they answer the query
   * the reader confirmed, exactly as a fresh Enter would. `null` when no Enter is
   * pending.
   */
  const [pendingEnter, setPendingEnter] = useState<{ query: string; index: number; key: string } | null>(null)

  const use = useCallback((id: string) => setUsage(prev => recordUse(id, Date.now(), prev)), [setUsage])

  const enterScope = useCallback((view: Scope, keepQuery: string) => {
    setScope(view)
    setQuery(keepQuery)
    setDebounced(keepQuery)
    setSelected(0)
    inputRef.current?.focus()
  }, [setScope, setQuery, setDebounced, setSelected, inputRef])

  /**
   * Create a session, put `text` in it, and go there.
   *
   * ONE copy, shared by the ask row and the three bulk modes, because the ordering
   * here is the whole correctness of the thing and a second copy would be a second
   * place for it to rot: create without activating, take the claim, activate, seed,
   * navigate — and re-check the claim after every await.
   *
   * A NEW session, never the active one's composer: `ChatPage` consumes
   * `pendingInput` by REPLACING the slot's draft and persisting it, so seeding the
   * current slot would destroy a half-written message. These rows also fire from
   * anywhere in the dashboard, where the active session may be one the user last
   * touched hours ago.
   *
   * `autoSend` is what separates a question from a command. The ask row hands over
   * a sentence the user wrote and stops at a filled composer, so they can still
   * edit it. A bulk mode's text is not theirs to edit — it is generated from a row
   * they picked and a link they pasted, and the argument step was the deliberate
   * act — so it sends.
   *
   * The steps are run here rather than through the shared `newSessionWithToken`
   * because that helper is fire-and-forget: its failure path is a `console.error`,
   * so a gateway that refuses the create would leave the bar closing on nothing and
   * the user's text gone. Both callers carry something they cannot retype from
   * memory, so the bar closes only once the session exists, and a rejection keeps
   * it open with the field intact.
   */
  const seedNewSession = useCallback(
    (pendingKey: string, text: string, failureLabel: string, autoSend: boolean, agent?: string, appName?: string) => {
      const run = dialogRunRef.current
      const owned = () => dialogRunRef.current === run
      // Whether this seed belongs to a CONTRIBUTED command, decided before the awaits.
      // The Ask row uses this same path and is never in the map, so it is unaffected.
      const contributed = commandByIdRef.current.has(pendingKey)
      // The folder this session will be filed into, read BEFORE the awaits for the
      // same reason `contributed` is: the app can be disabled mid-flight, and the
      // filing below must not depend on the row still being in the map.
      const folderName = commandFolderName(commandByIdRef.current, pendingKey)
      // Still offered by an enabled app? `owned()` tracks the dialog's own lifetime and
      // cannot see this: the app can be disabled from the Apps page while the session
      // create is still in flight, which leaves the run legitimately owned and the
      // command gone. Checked after every await, because that is the window.
      const stillOffered = () => !contributed || commandByIdRef.current.has(pendingKey)
      setPendingRow(pendingKey)
      // `activate: false` is what makes the rest of this safe, and it exists for
      // exactly this shape: the thunk creates the session WITHOUT stealing focus so a
      // caller that must finish setting the slot up can do so before the user is able
      // to type into it. Leaning on "create makes the new slot active" is only true at
      // the instant it resolves -- and this callback can resolve long after the user
      // has moved on, at which point the seed lands in whatever they moved to.
      // The contributed row may name the agent the seeded session runs as; ''
      // and undefined both mean the dashboard default. A named agent is always
      // selected as a TEMPLATE: it is one the app ships, and without the kind a
      // host crew member sharing that alias would win resolution and run the
      // app's prompt with the member's template and private memory. The owning app
      // rides along so the backend refuses any turn on which the bare name would
      // resolve to a different spec (a project-local one, say).
      void dispatch(createSlot({
        activate: false,
        sessionFolder: false,
        ...(agent ? { agent, agent_kind: 'template' as const, ...(appName ? { appAgentOwner: appName } : {}) } : {}),
        ...(contributed ? { memory_mode: 'persistent' } : {}),
      }))
        .unwrap()
        .then(
          async slot => {
            // The guard is released in `finally`, AFTER every await. Releasing it
            // earlier leaves it open across the awaits, so a second Enter during a slow
            // slot fetch starts a second create -- two sessions from one intent, which
            // is the exact failure the guard exists to prevent.
            try {
              if (!owned() || !stillOffered()) return
              // `keepTargetOnMissing`: this slot was JUST created, so a 404 from its
              // own detail fetch is a create/fetch race on a slot that does exist.
              // Without the opt-out, `switchSlot.rejected` treats the 404 as "target is
              // gone" and puts `activeSlot` back to where it came from (#6309) -- and
              // the seed below would then land in the reader's PREVIOUS conversation
              // and, with autoSend, fire there. A contributed prompt is typically an
              // instruction to act on a list of pull requests; running it against the
              // wrong session is the worst outcome this path has.
              //
              // With the opt-out the reducer keeps the fresh slot selected atomically,
              // so the rejection needs no repair from here and stays ignored: the
              // activation held either way.
              await dispatch(switchSlot({ key: slot.key, keepTargetOnMissing: true }))
                .unwrap()
                .catch(() => {})
              // Re-checked: the switch is another await, and the bar is still
              // dismissable across it. `stillOffered` too -- this is the last instant
              // before app-authored text becomes a message, and a disable that landed
              // during the switch must stop it here.
              if (!owned() || !stillOffered()) return
              dispatch(setPendingInput(text))
              // `autoSend=1` alone, never with `newSession=1`: the session already
              // exists -- we just created and activated it -- and asking ChatPage to
              // force a new one would land the text in a second, different session.
              navigate(autoSend ? '/chat?autoSend=1' : '/chat')
              onClose()
              // Filed LAST, and deliberately not awaited. A contributed row opens a new
              // session on every run, so unfiled they bury the reader's own chats and two
              // commands' runs interleave with nothing between them -- but the text is
              // already seeded by this point, so a slow, capped or refused folder API can
              // only cost this session its place in the sidebar. Contributed rows only:
              // the Ask row carries a sentence the reader wrote and belongs wherever they
              // are working, not in a folder named after a command.
              if (contributed && folderName) {
                void fileSessionInCommandFolder(
                  slot.key,
                  folderName,
                  Array.isArray(chatFoldersRef.current)
                    ? (chatFoldersRef.current as ChatFolderRow[])
                    : undefined,
                  // A folder this run created is not in the cache it just read, and the
                  // WebSocket push that would seed it is not guaranteed to arrive. Left
                  // uninvalidated, the sidebar can keep rendering a tree without the new
                  // folder and the next run reads the same stale list.
                  // Through the idle gate so the refetch cannot land inside a
                  // pending optimistic folder write in the sidebar.
                  () => invalidateFoldersWhenIdle(queryClient),
                )
              }
            } finally {
              // Only the OWNING run may clear the guard. Unconditionally, a stale
              // activation clears a LIVE one's: close and reopen during create A, start
              // create B, then let A resolve -- A's finally wipes B's `pendingRow`, and
              // the next Enter starts a second create for the same intent, which with
              // autoSend is a duplicate session that sends. `exitArgumentState` clears it
              // when it revokes, so a revoked run cannot leave the guard stuck either.
              if (owned()) setPendingRow(null)
            }
          },
          () => {
            if (!owned()) return
            setPendingRow(null)
            setActionError(i18nT('apps.commandBar.action_failed', { action: failureLabel }))
          },
        )
    },
    [dispatch, navigate, onClose, queryClient, dialogRunRef, commandByIdRef, setPendingRow, chatFoldersRef, setActionError],
  )

  const activateRoot = useCallback(
    (row: RankedRow) => {
      // A second Enter while the first activation is still resolving would run the
      // work twice -- two sessions from one intent -- because the bar stays open
      // until the promise settles.
      if (pendingRow) return
      // Revealing the rest of a list is not a use of any one row, so it records no
      // frecency and leaves the bar open on the rows it revealed.
      if (row.expand) {
        row.expand()
        return
      }
      use(row.id)
      if (row.kind === 'view') {
        // Entering is the activation event: the engine's first query happens
        // here, not while the user was still typing in the root.
        enterScope((row.view as Scope) ?? null, '')
        return
      }
      if (row.kind === 'prompt') {
        const cmd = commandById.get(row.id)
        // A row whose contribution is gone (the app was disabled while the bar was
        // open) must do nothing rather than fall through to the `invoke` branch and
        // silently close as if it had worked.
        if (!cmd) return
        if (!cmd.argument) {
          // Nothing to collect, so this is the whole action: seed and go.
          seedNewSession(cmd.id, cmd.prompt, cmd.title, cmd.autoSend, cmd.agent, cmd.appName)
          return
        }
        // No work yet -- this row's operation is defined by a value the user has not
        // given. The query is cleared because what they typed was the row's NAME, and
        // leaving it in a field that now means "paste the link" would read as a value
        // already supplied.
        setArgCommand(cmd)
        setQuery('')
        setDebounced('')
        setSelected(0)
        inputRef.current?.focus()
        return
      }
      if (row.kind === 'navigate' && row.route) {
        navigate(row.route)
        onClose()
        return
      }
      // An `invoke` row may do work that fails. Closing first would tell the user
      // it succeeded -- a new session that was never created looks identical to a
      // created one once the bar is gone -- so the bar closes only after the work
      // resolves, and a rejection keeps it open carrying the error.
      const pending = row.run?.()
      if (pending) {
        setPendingRow(row.id)
        void pending.then(
          () => {
            setPendingRow(null)
            onClose()
          },
          () => {
            setPendingRow(null)
            // Name the row and the way out: the bar deliberately stays open so Enter
            // retries, but that is invisible unless the copy says so.
            setActionError(i18nT('apps.commandBar.action_failed', { action: row.title }))
          },
        )
        return
      }
      onClose()
    },
    [commandById, enterScope, navigate, onClose, pendingRow, seedNewSession, use, setArgCommand, setQuery, setDebounced, setSelected, inputRef, setPendingRow, setActionError],
  )

  /**
   * Activate the row at `index`.
   *
   * `via` says WHICH GESTURE asked, because one of the guards below is true of only one
   * of them. A pointer names its own target: the reader pressed the row they could read,
   * and that row opens what it says even if the list is about to be replaced. A keyboard
   * Enter names an INDEX, and an index means a different row the moment the rows change
   * under it -- which is the whole hazard the stale-row guard exists for.
   */
  const activateIndex = useCallback(
    (index: number, via: 'key' | 'pointer') => {
      const slot = slots[index]
      if (!slot) return
      // STALE ROWS MUST NOT ACT, whatever they are -- see `rowsAnswerTheQuery`.
      //
      // Above the switch rather than inside `case 'result':`, because the rows a scoped
      // view synthesizes are built from the same debounced query its results are, and
      // two of them do something WORSE than opening the wrong thing. `clear-query*`
      // wipes the query; `open-crewmates` navigates and closes the bar. Both sit at a
      // zero-result dead end, which is exactly where a reader types another character,
      // so a guard that covered results only discarded the text they had just typed --
      // and the stale row's own label still showed the old query, so nothing warned
      // them. The `root` has no scope, so this is inert there, which is correct: it
      // ranks from the live query and has nothing stale to act on.
      //
      // The window does not EAT the Enter, it holds it: a keyboard confirm in here is
      // latched and fired once the rows answer the query it was pressed against (see
      // `pendingEnter` and the effect that drains it). The confirmed query and the
      // selected row are captured here, so a later keystroke or arrow does not ride
      // the same latch into a row the reader never confirmed. An arrowed row's own
      // stable key is captured alongside its index, because a cached new query can
      // swap its rows in under the same index with no empty frame, and the drain fires
      // a non-top row only while its key still matches rather than whatever now sits at
      // that number.
      // Type-then-Enter is the
      // launcher's primary gesture, and dropping it left a fast typist pressing Enter
      // at a silent bar. A POINTER is never latched -- a click names the row it is on,
      // which opens what it says -- and it also drops any armed latch, so a click that
      // enters a scope without closing the bar cannot leave a keyboard confirm behind.
      if (via === 'pointer') setPendingEnter(null)
      if (via === 'key' && scope && !rowsAnswerTheQuery) {
        setPendingEnter({ query: query.trim(), index, key: slot.key })
        return
      }
      switch (slot.tag) {
        case 'root':
          activateRoot(slot.row)
          return
        case 'result':
          slot.row.onActivate()
          onClose()
          return
        case 'fallback':
          enterScope('sessions', query)
          return
        case 'fallback-artifacts':
          enterScope('artifacts', query)
          return
        case 'fallback-folders':
          enterScope('folders', query)
          return
        case 'fallback-mates':
          enterScope('mates', query)
          return
        case 'ask': {
          // Stops at a FILLED composer rather than sending: the user wrote this
          // sentence, so the last look at it is theirs.
          if (pendingRow) return
          seedNewSession(
            ASK_SLOT_KEY,
            query.trim(),
            i18nT('apps.commandBar.action_ask'),
            false,
          )
          return
        }
        case 'recovery':
          // Offer the way back rather than only describing it.
          navigate('/apps/detail/command-bar')
          onClose()
          return
        case 'retry':
          void refetchSessions()
          return
        case 'retry-artifacts':
          void refetchArtifacts()
          return
        case 'retry-folders':
          void refetchFolders()
          return
        case 'retry-mates':
          void refetchMates()
          return
        case 'open-crewmates':
          // The page where a crewmate is made. No `?member=`: there is none to open.
          navigate('/members')
          onClose()
          return
        case 'clear-query':
        case 'clear-query-folders':
        case 'clear-query-mates':
          // Emptying the query is what re-arms the listing; the debounced copy has to
          // go with it or the view stays on the failed search for one more tick.
          setQuery('')
          setDebounced('')
          setSelected(0)
          inputRef.current?.focus()
          return
      }
    },
    [activateRoot, enterScope, navigate, onClose, pendingRow, query, refetchArtifacts, refetchFolders, refetchMates, refetchSessions, rowsAnswerTheQuery, scope, seedNewSession, slots, setQuery, setDebounced, setSelected, inputRef],
  )

  /**
   * Fire a latched Enter once the rows answer the confirmed query, or drop it.
   *
   * The latch is set when a keyboard Enter lands in the debounce window (above), and
   * it carries the query the reader confirmed and the row they had selected. It drains
   * here rather than in the key handler because the thing it waits for -- the rows
   * catching up -- is an async settle, not a keystroke.
   *
   * It fires only when all of these still hold; otherwise it is dropped:
   *  - the reader has not typed on: the live query still equals the confirmed one.
   *    Typing another character confirms a different query, which this latch never
   *    stood for, so it is dropped rather than committed.
   *  - the selection has not moved: an arrow or a hover that moved `selected` after
   *    the Enter means the reader is aiming elsewhere, so the stale row is not fired.
   *  - the row at that index is still the one the reader confirmed, for a NON-top
   *    selection: row 0 is the primary gesture (open the best match for the typed
   *    query) and fires the live top row, but an arrowed row carries the key it was
   *    picked at, so a cached new query that swaps rows under the old indices drops
   *    the latch rather than open a stranger at that number.
   *  - the view has settled on rows that answer that query, and the selected row is a
   *    RESULT. A no-match drop row, an empty-roster row, or a failed-read retry wipes
   *    the query or navigates away, which is not what a reader who typed a name and
   *    pressed Enter asked for; the latch drops. Visible-stable, per the one
   *    constraint the reporter named.
   */
  useEffect(() => {
    if (!pendingEnter) return
    if (!scope) {
      setPendingEnter(null)
      return
    }
    // The reader typed on, or moved the selection: the live gesture differs from the
    // one that was confirmed.
    if (query.trim() !== pendingEnter.query || selected !== pendingEnter.index) {
      setPendingEnter(null)
      return
    }
    // Still settling, or the rows still answer an older query: hold.
    if (scopeLoading || scopeFetching || !rowsAnswerTheQuery) return
    const row = slots[pendingEnter.index]
    if (row?.tag !== 'result') {
      // Settled on a dead end: nothing the gesture meant to open.
      setPendingEnter(null)
      return
    }
    // Row 0 is the primary gesture — "open the best match for what I typed" — so it
    // fires the live top row even though its content swapped to answer the new query.
    // A NON-top index means the reader arrowed onto one specific row, so the row now
    // at that index must still be the one they picked: a cached new query swaps rows
    // under the old indices with no empty frame, and firing a changed row there opens
    // a stranger, the wrong-row hazard this latch exists to close.
    if (pendingEnter.index !== 0 && row.key !== pendingEnter.key) {
      setPendingEnter(null)
      return
    }
    setPendingEnter(null)
    activateIndex(pendingEnter.index, 'key')
  }, [pendingEnter, scope, query, selected, scopeLoading, scopeFetching, rowsAnswerTheQuery, slots, activateIndex])

  /**
   * Drop the latch the moment the reader leaves the window any other way.
   *
   * Escape out of the scope and Backspace out of it both land on `scope === null`;
   * closing the bar flips `open`. The drain effect above already refuses to fire
   * without a scope, but clearing here as well keeps a stale latch from riding a
   * reopen into the next visit.
   */
  useEffect(() => {
    if (!open || !scope) setPendingEnter(null)
  }, [open, scope])

  return { seedNewSession, activateIndex }
}
