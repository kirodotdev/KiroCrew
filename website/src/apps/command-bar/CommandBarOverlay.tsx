import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { OverlayPortal } from '../../components/overlayRoot'
import { Command, Loader2 } from 'lucide-react'

import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { selectSlotsWithLiveLastTs } from '../../store/slotLastTs'
import { selectPersonSlots } from '../../store/dashboardSlice'
import { findReport } from '../../utils/errorReport'
import { errMessage } from '../../utils/thunkError'
import ErrorNotice from '../../components/ErrorNotice'
import { useSettingsSearchGovernance } from '../../components/commandPalette/useSettingsSearchGovernance'
import { usePaletteActions } from '../../components/commandPalette/paletteActions'
import { resolveCopyTarget, type CopyableRow } from '../../components/commandPalette/copyTarget'
import { copyToClipboard } from '../../utils/clipboard'
import { buildShareableUrl } from '../../utils/shareUrl'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useVisualViewport } from '../../hooks/useVisualViewport'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useTheme } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import { useLanguage } from '../../i18n/LanguageProvider'

import { loadUsage, type UsageMap } from './frecency'
import {
  argumentIsValid,
  contributedCommands,
  resolvePrompt,
  type ContributedCommand,
} from './contributedCommands'
import { useImeGuard } from '../../hooks/useImeGuard'
import { usePointerMotion } from '../../hooks/usePointerMotion'
import { ARTIFACTS_ROW_LIMIT, COPY_KEY, DEBOUNCE_MS, ENTER_KEY, SKELETON_WIDTHS, actionLabel, headerOf, rootRowCopyable, type Scope } from './commandBarOverlayModel'
import { useCommandBarOverlayEngines } from './useCommandBarOverlayEngines'
import { useCommandBarOverlayRootRows } from './CommandBarOverlayRootRows'
import { crewWaitingApprovals } from '../../pages/chat/command-center/model'
import { useCrewMode } from '../../hooks/usePreviewFlag'
import { useCommandBarOverlaySlots } from './useCommandBarOverlaySlots'
import { useCommandBarOverlayActivation } from './useCommandBarOverlayActivation'
import { commandBarOverlaySlotRenderer } from './CommandBarOverlaySlot'

/**
 * Command Bar — the ⌘K launcher.
 *
 * Contributed by the `command-bar` app as an overlay claiming the host's
 * `quick-search` slot, so the host renders it only while that app is enabled.
 *
 * The shape it is built around: the FIRST PAGE is a launcher over rows already in
 * memory (commands, app destinations, quicklinks, settings) and never queries a
 * backend, so typing in it costs nothing no matter how much history the instance
 * holds. A content search is a ROW you enter — entering it is the activation
 * event that lets that engine run its first query. The previous surface fanned
 * every keystroke out to every provider, which is why typing could stall the
 * gateway's event loop; here a keystroke in the root has nothing to fan out to.
 *
 * No prefix sigils: the entry gesture is Enter on a row, and habit (frecency
 * ranking) is what makes a frequent row reachable in one or two keystrokes.
 */

export default function CommandBarOverlay({
  open,
  onClose,
  initialQuery = '',
}: {
  open: boolean
  onClose: () => void
  initialQuery?: string
}) {
  const ime = useImeGuard()
  const vv = useVisualViewport()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  // Seeded from what the host caught while this chunk loaded, so keys typed right
  // after the gesture rank the first render instead of being lost. The host mounts
  // the bar once per open, so this is also what makes each open start fresh.
  const [query, setQuery] = useState(initialQuery)
  const [debounced, setDebounced] = useState(initialQuery)
  const [scope, setScope] = useState<Scope>(null)
  /**
   * The contributed command whose ARGUMENT the field is currently collecting.
   *
   * A second navigation state beside `scope`, and deliberately not folded into it:
   * a scope is a place to search, this is a question being asked, and the two
   * differ in what Enter means. They share the rest of the contract — the chip
   * naming where you are, Escape and Backspace stepping back out — so the sites
   * below read `scope ?? argCommand` rather than growing a second copy of each.
   *
   * Holds the whole command rather than an id: it carries the placeholder, the
   * pattern and the prompt template, and re-deriving those from the app list on
   * every keystroke would let a mid-flight app disable change what the field the
   * reader is typing into is about to run.
   */
  const [argCommand, setArgCommand] = useState<ContributedCommand | null>(null)
  //
  // Whether the resolved-prompt preview is taller than its box. Measured, not derived
  // from the prompt's length: wrapping is what decides overflow, so a character or
  // line count would both over- and under-report. Only used to warn that the
  // instruction continues out of sight -- never to gate the send, which stays the
  // reader's call.
  const previewRef = useRef<HTMLPreElement | null>(null)
  const [previewClipped, setPreviewClipped] = useState(false)

  const [selected, setSelected] = useState(0)
  /**
   * Hover selects a row only on REAL pointer motion, so a row re-ranked under a
   * resting pointer cannot overwrite the reset to row 0 that every keystroke makes
   * (see `usePointerMotion`). The trail is cleared each time the bar opens.
   */
  const pointerMoved = usePointerMotion(open)
  const selectOnPointerMove = (i: number, e: { clientX: number; clientY: number }) => {
    if (pointerMoved(e)) setSelected(i)
  }
  /**
   * Whether the reader asked to see every session waiting on them. Until then the
   * attention group keeps the root's per-group cap and ends on a row counting the
   * rest, so a seventh blocked session is never dropped without a trace.
   */
  const [attentionExpanded, setAttentionExpanded] = useState(false)
  const [usage, setUsage] = useState<UsageMap>(() => loadUsage())
  // The rows every other settings search withholds, withheld here too, from
  // cached answers only: the root issues no request.
  const settingsGovernance = useSettingsSearchGovernance({ fetch: false })
  const [actionError, setActionError] = useState<string | null>(null)
  /**
   * What the last copy did, held until the reader moves.
   *
   * A copy is the one action on this surface with NO observable result: the bar looks
   * identical afterwards, the clipboard is not visible, and the reader finds out
   * whether it worked when they paste somewhere else. So it is stated here, and it is
   * stated from `copyToClipboard`'s own return value rather than from having called
   * it -- that helper explicitly warns that a tick over an unchanged clipboard is
   * worse than no affordance at all, and it fails for real on a plain-HTTP LAN
   * gateway, where the async clipboard API is unavailable.
   */
  const [copyNotice, setCopyNotice] = useState<{ text: string; what?: string } | null>(null)
  /**
   * A clipboard write that did NOT land.
   *
   * Held apart from `copyNotice` because it is a different kind of thing: a failed
   * write is an error, and an error renders through `ErrorNotice` like every other one
   * on this surface. A copy with no target is not an error -- the reader asked a row
   * with no address for its address -- so it stays on the status line.
   *
   * Carries `what` as well as the message, because the message names a REMEDY -- select
   * the text and copy it by hand -- and this is the one path where the text is not on
   * screen: the clipboard refused, so nothing else put it there. An instruction that
   * points at nothing is worse than no instruction.
   */
  const [copyError, setCopyError] = useState<{ text: string; what: string } | null>(null)
  /** Row id whose `invoke` work is still resolving, or null. */
  const [pendingRow, setPendingRow] = useState<string | null>(null)
  /**
   * Generation of the current dialog session, bumped every time the bar closes.
   *
   * An ask that is still creating its session when the user dismisses the bar has
   * lost its claim on the dashboard: seeding a composer and navigating at that point
   * writes into whatever the user moved on to. `ChatPage` guards the same class of
   * race with its own `ownsLifecycle()` check and states the trade there -- an
   * abandoned request may leave an unused server slot, but it must not write shared
   * state or steal focus from its successor. This is that guard for this surface.
   *
   * While the bar is OPEN it cannot go stale: the dialog is modal with a real focus
   * trap, so the only thing that can change the active session in that window is our
   * own create.
   */
  const dialogRunRef = useRef(0)

  const { navigate } = usePaletteActions()
  const { resolved } = useLanguage()
  const { cycle: cycleTheme } = useTheme()
  const dispatch = useAppDispatch()
  // A store HANDLE for the session rows' post-switch focus: by the time a
  // `switchSlot` settles this bar has closed, so only the store can still say
  // which slot is active. Read, never subscribed -- the selectors below are the
  // subscriptions.
  const store = useAppStore()
  // Live session state, read straight from the store the dashboard already keeps
  // current over its socket. This is what lets the root LEAD with the sessions that
  // owe the user something without issuing a request: the facts are already here,
  // and the alternative surfaces (sidebar, recents) are reading the same three.
  const liveSlots = useAppSelector(selectSlotsWithLiveLastTs)
  // The person's own sessions: a Crew Mode chat's workers are listed in its side
  // panel, never in the Needs You or Recent sessions rows (the full set stays for
  // the engines, which key navigation and the mates' running state on it).
  const personSlots = useAppSelector(selectPersonSlots)
  // What those hidden workers owe the person, carried by their root: a worker's
  // tool approval is counted on its conductor over the FULL slot set, so Needs
  // You still names it (the fleet page counts the same way, `crewWaitingApprovals`).
  const crewMode = useCrewMode()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- crewMode stands for the preview crewWaitingApprovals reads
  const crewWaiting = useMemo(() => crewWaitingApprovals(liveSlots), [liveSlots, crewMode])
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  const slotStatusDetail = useAppSelector(s => s.chat.slotStatusDetail ?? {})
  const subagentQueued = useAppSelector(s => s.chat.subagentQueued)
  const simplifiedToolNames = useSimplifiedToolNames()
  // `aria-modal` is a promise that Tab cannot reach the page behind the dialog, so
  // the trap has to be real. Escape is owned by the dialog panel's own keydown
  // handler below, which needs it to pop a scope before it closes the bar (and
  // claims it through the IME latch first).
  useDialogFocusTrap(dialogRef, onClose, { handleEscape: false })
  const {
    sessions,
    openSessionsKey,
    recentSessions,
    apps,
    chatFoldersRef,
    queryClient,
    folderSortMode,
    folderSortError,
    artifacts,
    folders,
    runningMates,
    mates,
  } = useCommandBarOverlayEngines({ scope, liveSlots, subagentQueued, dispatch, navigate })

  // The query is NOT reset here. It is seeded at mount, and clearing it on the open
  // edge would drop the type-ahead along with anything typed before this effect ran.
  useEffect(() => {
    if (!open) return
    setScope(null)
    setArgCommand(null)
    setSelected(0)
    setUsage(loadUsage())
    setActionError(null)
    setPendingRow(null)
    setAttentionExpanded(false)
  }, [open])

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [query])

  // Closing is what revokes an in-flight activation's claim. Bumped on the CLOSE edge
  // rather than the open one so work started in this session is invalidated the
  // moment the user walks away from it, not later when they happen to come back.
  useEffect(() => {
    if (!open) dialogRunRef.current += 1
  }, [open])

  // Teardown revokes too. The effect above is keyed on the `open` PROP, and an
  // unmount never sets it false -- the component is simply gone, its effects never
  // run again, and the in-flight callback still holds the ref OBJECT, so it would
  // compare equal and pass its own guard. This is the case a host that renders the
  // overlay conditionally produces, and it is the one a review found after the prop
  // edge was already covered.
  useEffect(() => () => {
    dialogRunRef.current += 1
  }, [])

  /**
   * Leave the argument state, revoking any activation started from it.
   *
   * Bumping `dialogRunRef` is the load-bearing half. The two effects above revoke on the
   * same principle they state — work is invalidated the moment the user walks away from
   * it — and stepping back OUT of the argument state is walking away just as much, only
   * at a narrower scope: the bar stays open. Without the bump, Enter on a slow session
   * create followed by Escape leaves that create in flight, and it resolves into a
   * seeded, auto-sent session the reader had already cancelled.
   *
   * One function rather than the increment repeated at each exit, because the failure
   * mode is an exit path that forgets it — which is exactly how this shipped: two revoke
   * sites existed and all three argument exits had none.
   */
  const exitArgumentState = useCallback(() => {
    dialogRunRef.current += 1
    setArgCommand(null)
    setActionError(null)
    setSelected(0)
    // Revoking is what makes the in-flight run stale, and a stale run no longer clears
    // its own guard, so the guard has to be released here or a revoked activation would
    // leave the bar permanently refusing the next Enter.
    setPendingRow(null)
  }, [])

  // A live view of the contributed commands for the ASYNC seeding path. The memo itself
  // is captured by value in that closure, so after an await it describes the apps as they
  // were when the row was activated -- which is the window this ref exists to close.
  const commandByIdRef = useRef<Map<string, ContributedCommand>>(new Map())

  // A failure describes the row the user just activated, so it must not outlive the
  // query that produced it. The in-flight guard is deliberately NOT cleared here:
  // typing while work is resolving must not re-arm a second activation of it.
  useEffect(() => {
    setActionError(null)
  }, [query])

  /**
   * Contributed commands by row id.
   *
   * The rows carry only what ranking needs; activation needs the prompt template,
   * the argument spec and the autoSend flag, so it resolves the row back to its
   * contribution here rather than the row model growing app-specific fields.
   */
  const commandById = useMemo(() => {
    const map = new Map<string, ContributedCommand>()
    for (const cmd of contributedCommands(apps ?? [])) map.set(cmd.id, cmd)
    return map
  }, [apps])
  commandByIdRef.current = commandById

  const ranked = useCommandBarOverlayRootRows({
    liveSlots: personSlots, crewWaiting, unreadSlots, slotStatusDetail, simplifiedToolNames, dispatch, navigate, store, cycleTheme,
    commandById, apps, settingsGovernance, resolved, scope, query, usage, attentionExpanded,
    setAttentionExpanded,
  })

  const {
    scopedQuery, sessionsSearchError, refetchSessions,
    artifactsQuery, artifactRows, artifactsSearchError, artifactsError, refetchArtifacts,
    folderQuery, foldersError, foldersSearchError, refetchFolders,
    mateQuery, matesError, matesSearchError, refetchMates,
    rowsAnswerTheQuery, slots, rowCount, scopeLoading, scopeFetching,
  } = useCommandBarOverlaySlots({
    scope, debounced, query, sessions, openSessionsKey, recentSessions, artifacts, folderSortMode, folders,
    runningMates, mates,
    argCommand, ranked, selected, setSelected,
  })

  const { seedNewSession, activateIndex } = useCommandBarOverlayActivation({
    open, onClose, scope, setScope, query, setQuery, setDebounced, selected, setSelected, inputRef,
    setUsage, setArgCommand, pendingRow, setPendingRow, setActionError, dialogRunRef,
    commandById, commandByIdRef, chatFoldersRef, dispatch, navigate, queryClient,
    slots, rowsAnswerTheQuery, scopeLoading, scopeFetching,
    refetchSessions, refetchArtifacts, refetchFolders, refetchMates,
  })

  /**
   * Put the selected row's address on the clipboard.
   *
   * Returns whether the gesture was CLAIMED, so the key handler can decline ⌘C in the
   * one state where the bar has no row to answer with and let the browser's own copy
   * run instead.
   *
   * The address itself is resolved, never stored per row: `resolveCopyTarget` reads
   * what the row already says about where it points. That is the whole reason this is
   * a layer rather than a command -- a launcher that lists sessions, artifacts, pages
   * and settings can copy all four without any of the four knowing about copying, and
   * the next corpus added to the bar arrives copyable.
   */
  const copyTarget = useMemo(() => {
    const slot = slots[Math.min(selected, Math.max(0, slots.length - 1))]
    if (!slot) return null
    const row: CopyableRow | null =
      slot.tag === 'root' ? rootRowCopyable(slot.row) : slot.tag === 'result' ? slot.row : null
    if (!row) return null
    return resolveCopyTarget(row, {
      origin: window.location.origin,
      // The chat surface's own copy button builds the session link with this, and one
      // builder is the point: two would drift into two different links for one session.
      sessionLink: buildShareableUrl,
    })
  }, [selected, slots])

  const copySelected = useCallback(() => {
    if (slots.length === 0) return false
    if (!copyTarget) {
      // Claimed anyway, and answered. A launcher row is a thing the reader pressed a
      // key at, so the honest reply to "copy this" is that this one has no address --
      // silence would read as a copy that worked.
      setCopyError(null)
      setCopyNotice({ text: i18nT('apps.commandBar.copy_nothing') })
      return true
    }
    setCopyNotice(null)
    setCopyError(null)
    void copyToClipboard(copyTarget).then(ok => {
      if (!ok) {
        setCopyError({ text: i18nT('apps.commandBar.copy_failed'), what: copyTarget })
        return
      }
      // The address travels with the confirmation. "Copied" alone left the reader to
      // find out WHICH address on paste, and the two are genuinely different things: a
      // deployed artifact yields its public URL where every other row yields a link
      // into this dashboard.
      setCopyNotice({ text: i18nT('apps.commandBar.copied'), what: copyTarget })
    })
    return true
  }, [copyTarget, slots.length])

  // The outcome describes ONE row's copy, so it must not outlive the reader's attention
  // on that row: a "Copied" line still sitting there after they arrow somewhere else
  // reads as a claim about the row they arrived at.
  useEffect(() => {
    setCopyNotice(null)
    setCopyError(null)
  }, [selected, query, scope])

  /**
   * Enter in the argument state: check the value, then hand the command to a session.
   *
   * The check runs HERE, against the pattern the CONTRIBUTION declared, because the
   * collected text is spliced into an instruction handed to an agent with tools. A
   * command that writes somewhere must not be handed the last thing the reader
   * happened to copy, and the field they are still looking at is the cheapest place
   * in the system to refuse it. The app supplies the error message, since only the
   * app knows what shape it wanted.
   */
  // Re-measured on every change to what is previewed, since the same prompt clips or
  // does not depending on the value spliced into it -- AND on every change to the box it
  // is measured in. Content is not the only input: narrowing the viewport rewraps the
  // text, so a prompt that fitted starts clipping with the cue absent, which is the
  // unsafe direction (with autoSend, Enter then sends a tail the reader never saw).
  // A ResizeObserver rather than a window listener, because the box also moves when a
  // font finishes loading or the dialog reflows, and neither raises a resize event.
  useEffect(() => {
    const el = previewRef.current
    if (!el) {
      setPreviewClipped(false)
      return
    }
    const measure = () => setPreviewClipped(el.scrollHeight > el.clientHeight + 1)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [argCommand, query])

  const submitArgument = useCallback(() => {
    if (!argCommand || pendingRow) return
    // Re-resolved from the CURRENT contributions rather than trusting the snapshot
    // taken when the field opened. The field stays open across an arbitrary pause --
    // the reader is pasting a link -- and `apps` can change underneath it: the app can
    // be disabled or uninstalled from the Apps page in another tab, or by a gateway
    // event. The row vanishes from the list immediately, but this captured object
    // would not, so submitting would send the prompt of an app the reader had just
    // switched off. Re-resolving also picks up an edited prompt or a narrowed matcher
    // instead of acting on the version captured minutes ago.
    const live = commandById.get(argCommand.id)
    if (!live) {
      // Revokes too: the app is gone, so anything already in flight from this field
      // must not land either.
      exitArgumentState()
      setActionError(i18nT('apps.commandBar.argument_withdrawn'))
      return
    }
    if (!argumentIsValid(live, query)) {
      setActionError(live.argument?.patternError || i18nT('apps.commandBar.argument_invalid'))
      return
    }
    // The DISPLAYED command's matcher has to accept as well, not just the live one. The
    // preview is withheld until the value validates, so if the app broadened its matcher
    // while the field was open -- `url` with a host allowlist to `text`, say -- the
    // reader has been looking at a rejection the whole time and never saw a preview,
    // while the live matcher now passes. The prompt itself may be unchanged, so the
    // comparison below cannot catch it: what changed is whether anything was shown.
    if (!argumentIsValid(argCommand, query)) {
      setArgCommand(live)
      setActionError(i18nT('apps.commandBar.argument_changed'))
      return
    }
    // What was SHOWN has to be what is sent. Re-resolving above fixed a stale snapshot
    // firing after its app was disabled, but it introduced the mirror hazard: the
    // preview renders `argCommand`, so if the app's prompt or its autoSend changed while
    // the field was open, the reader would be consenting to text that is no longer the
    // text that goes out. Compared by resolved VALUE, not object identity -- the
    // contribution list is rebuilt on every apps refresh, so identity differs even when
    // nothing about the command did, and identity comparison would demand a second Enter
    // for no reason.
    //
    // On divergence the preview is refreshed and nothing is sent: the next Enter acts on
    // what is now on screen. Deliberately not a silent swap to the new prompt, which is
    // the whole finding, and deliberately not a refusal either -- the command is fine,
    // it just changed, and one keystroke re-consents.
    const shown = resolvePrompt(argCommand, query)
    const now = resolvePrompt(live, query)
    if (now !== shown || live.autoSend !== argCommand.autoSend) {
      setArgCommand(live)
      setActionError(i18nT('apps.commandBar.argument_changed'))
      return
    }
    setActionError(null)
    seedNewSession(live.id, now, live.title, live.autoSend, live.agent, live.appName)
  }, [argCommand, commandById, exitArgumentState, pendingRow, query, seedNewSession])

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelected(i => (rowCount === 0 ? 0 : (i + 1) % rowCount))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelected(i => (rowCount === 0 ? 0 : (i - 1 + rowCount) % rowCount))
      } else if (e.key === 'Enter') {
        // Only the Enter branch is claimed — arrow navigation stays untouched.
        if (!ime.claimEnter(e)) return
        // In the argument state Enter belongs to the FIELD, not to a row: there are
        // no rows, and what the user typed is the argument rather than a query.
        if (argCommand) {
          e.preventDefault()
          submitArgument()
          return
        }
        activateIndex(selected, 'key')
      } else if (
        (e.metaKey || e.ctrlKey) &&
        !e.altKey &&
        !e.shiftKey &&
        (e.key === 'c' || e.key === 'C')
      ) {
        // The launcher's copy gesture, on the chord every other application already
        // uses for copying, which is why it needs no affordance to teach.
        //
        // Declined in two states rather than claimed unconditionally. The input holds
        // focus the entire time the bar is open, so a reader who SELECTED part of what
        // they typed means that selection, and taking ⌘C from it would make this field
        // behave unlike every other text box on the machine. And the argument state
        // lists no rows at all -- what is on screen there is the value they are
        // pasting in, which is theirs for the same reason.
        const input = e.currentTarget
        const selecting = input.selectionStart !== null && input.selectionStart !== input.selectionEnd
        if (selecting || argCommand) return
        if (copySelected()) e.preventDefault()
      } else if (e.key === 'Backspace' && query === '' && (scope || argCommand)) {
        // Leaving a scope is Backspace on an empty input — the same gesture that
        // deletes a character, so it needs no separate key to learn. An argument
        // state leaves the same way: it is a place the user stepped into, and
        // abandoning the question must not also discard the whole bar.
        e.preventDefault()
        setScope(null)
        exitArgumentState()
      }
    },
    // No `onClose`: Escape belongs to the dialog below, so nothing in here
    // dismisses the bar. A dismissal reached from a row goes through
    // `activateIndex`, which is listed.
    [
      activateIndex,
      argCommand,
      copySelected,
      exitArgumentState,
      ime,
      query,
      rowCount,
      scope,
      selected,
      submitArgument,
    ],
  )

  if (!open) return null

  /**
   * The chip naming where the user is: a scope, or the mode asking for a link.
   *
   * One label for both states so the breadcrumb, its Escape handler and its
   * placeholder cannot disagree about which one is showing.
   */
  const navName = argCommand
    ? argCommand.title
    : scope === 'sessions'
      ? i18nT('apps.commandBar.cmd_search_sessions')
      : scope === 'artifacts'
        ? i18nT('apps.commandBar.cmd_search_artifacts')
        : scope === 'folders'
          ? i18nT('apps.commandBar.cmd_search_folders')
          : scope === 'mates'
            ? i18nT('apps.commandBar.cmd_search_mates')
            : ''
  /**
   * The failure this change is responsible for, and the only one that gets a notice.
   *
   * Each scope's retry ROW is pushed by that scope's own branch in the slot builder
   * (`useCommandBarOverlaySlots`), so there is no shared failure flag to key this on:
   * the notice belongs to the artifacts scope alone.
   */
  const artifactsFailed = scope === 'artifacts' && !!artifactsError
  const foldersFailed = scope === 'folders' && !!foldersError
  const matesFailed = scope === 'mates' && !!matesError
  const searchError =
    scope === 'sessions'
      ? sessionsSearchError
      : scope === 'artifacts'
        ? artifactsSearchError
        : scope === 'folders'
          ? foldersSearchError
          : scope === 'mates'
            ? matesSearchError
            : undefined
  /**
   * What the failure SAYS, for the scope this change adds.
   *
   * ARTIFACTS AND FOLDERS. The sessions scope keeps the failure row it already had:
   * its text was the static `search_failed`, never a backend string, so nothing about
   * it misled the reader whose report motivated the wording here -- that reader met
   * this view. Reshaping it would have been this PR changing a surface it does not
   * own, on a symmetry argument.
   *
   * The concrete rejection is deliberately NOT rendered. Reading it off the error
   * yielded backend wording like "gateway unavailable", which a first-time reader
   * read as the "Run a local gateway" setting and then would not touch -- at the
   * moment of failure that is a cause they cannot interpret next to a fix they are
   * afraid of. Its structured journal report is resolved from the raw rejection
   * and passed directly to `ErrorNotice` below rather than recovered from the
   * friendly rendered sentence. That preserves the endpoint, status, and backend
   * code without exposing backend wording in visible text, a tooltip, or an aria
   * label.
   */
  const searchFailedText = artifactsFailed
    ? i18nT('apps.commandBar.artifact_search_failed')
    : foldersFailed || matesFailed
      ? i18nT('apps.commandBar.search_failed')
      : ''
  /**
   * How many name matches the cap is hiding.
   *
   * The endpoint returns every match, so this count is the real remainder rather
   * than a page boundary. Without it the cap was the one silent state left in the
   * view: a name matching thirty artifacts drew twelve rows and said nothing.
   */
  const artifactOverflow = Math.max(0, (artifactRows?.length ?? 0) - ARTIFACTS_ROW_LIMIT)
  const listId = 'command-bar-list'
  const rowId = (i: number) => `command-bar-row-${i}`

  const renderSlot = commandBarOverlaySlotRenderer({
    query, scope, artifactsQuery, scopedQuery, folderQuery, mateQuery, pendingRow,
  })

  return <OverlayPortal>
    <div
      className="fixed left-0 right-0 z-[9999] flex items-start justify-center bg-bg/60 backdrop-blur-xs animate-rise"
      style={{ top: vv.offsetTop, height: vv.height }}
      // The backdrop is a click target for dismissal, not a control: the dialog role
      // belongs to the card below, and screen readers should skip this layer.
      role="presentation"
      // Dismiss only when the press lands on the backdrop ITSELF. Testing the target
      // beats stopping propagation on the card, which would put a mouse handler on a
      // non-interactive dialog element for no behavioural gain.
      onMouseDown={e => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- a dialog owns the keyboard dismissal of its own subtree; the handler below is Escape only, and every gesture inside is on a real control */}
      <div
        ref={dialogRef}
        // 680px, up from 576: at the narrower width a settings row's title and its
        // tab subtitle both truncated on a 1440 screen, which is the one thing that
        // column exists to prevent. The panel scales in over 200ms — the same entrance
        // the rest of the shell's dialogs use — because a launcher that hard-cuts into
        // place reads as a repaint rather than as a surface arriving. The global
        // reduced-motion rule zeroes its duration.
        className="w-full max-w-[680px] mx-4 bg-card border border-border rounded-xl shadow-xl overflow-hidden flex flex-col animate-scale-in"
        style={{ marginTop: Math.round(vv.height * 0.12), maxHeight: Math.round(vv.height * 0.7) }}
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('apps.commandBar.title')}
        // Escape belongs to the DIALOG, not the input. The focus trap's own Escape is
        // disabled because leaving a scope has to come first, and while the input was
        // the only focusable element putting the handler there was equivalent -- it is
        // not any more: Tab reaches the scope chip and the Retry button, and Escape
        // must dismiss from either. Keydown from the input bubbles here, so this is
        // one owner rather than two.
        onKeyDown={e => {
          if (e.key !== 'Escape') return
          // An Escape mid-composition belongs to the IME — it cancels the candidate
          // list, not the search scope or the bar. The claim owns the whole decline
          // contract (a declined key keeps its default for the IME and is stopped
          // from leaking to outer layers), so it must run BEFORE the unconditional
          // preventDefault below takes the key for the dialog.
          if (!ime.claimKey(e)) return
          e.preventDefault()
          // Inside a scope, Escape steps OUT of it rather than discarding the whole
          // search: the query the user typed is the expensive part, and Backspace on
          // an empty input is the only other way back, which nothing advertises.
          if (scope) {
            setScope(null)
            setSelected(0)
            inputRef.current?.focus()
            return
          }
          // Same for the argument state: the first Escape abandons the question, the
          // second closes the bar. A mode entered by mistake must not cost the user
          // the whole surface.
          if (argCommand) {
            exitArgumentState()
            inputRef.current?.focus()
            return
          }
          onClose()
        }}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
          <Command size={15} className="lucide-inline text-muted shrink-0" />
          {(scope || argCommand) && (
            <>
              <button
                type="button"
                onClick={() => {
                  setScope(null)
                  exitArgumentState()
                  inputRef.current?.focus()
                }}
                title={i18nT('apps.commandBar.leave_scope')}
                aria-label={i18nT('apps.commandBar.leave_scope')}
                // No fill and no tint. A filled accent pill sitting against an
                // unpainted field is the loudest thing on the surface, and it is
                // labelling the state the user just chose — the one thing they already
                // know. Weight and a separator carry the same information: this word is
                // where you are, what follows is what you type. The focus ring stays,
                // and unlike the field's it only ever paints on Tab, so it is never the
                // permanent box.
                className="shrink-0 max-w-[40%] truncate text-[13px] text-text bg-transparent border-none p-0 cursor-pointer focus-ring-accent rounded"
              >
                {navName}
              </button>
              <span aria-hidden className="shrink-0 text-muted select-none">
                ›
              </span>
            </>
          )}
          <input
            ref={inputRef}
            autoFocus
            value={query}
            // Deliberately NO `maxLength`. It looked like defence in depth and was the
            // opposite: the browser clips a paste to the limit BEFORE `onChange`, so a
            // 2001-character value arrived as a valid-looking 2000-character prefix and
            // was sent -- exactly the silent truncation the module refuses to do, snuck in
            // one layer below the check that refuses it. The validator sees the whole
            // value and rejects it, which is what lets the reader be told.
            onChange={e => {
              setQuery(e.target.value)
              setSelected(0)
            }}
            {...ime.bindComposition()}
            onKeyDown={onKeyDown}
            placeholder={
              argCommand
                ? argCommand.argument?.placeholder || i18nT('apps.commandBar.placeholder_argument')
                : scope === 'artifacts'
                  ? i18nT('apps.commandBar.placeholder_artifacts')
                  : scope === 'folders'
                    ? i18nT('apps.commandBar.placeholder_folders')
                    : scope === 'mates'
                      ? i18nT('apps.commandBar.placeholder_mates')
                      : scope
                        ? i18nT('apps.commandBar.placeholder_sessions')
                        : i18nT('apps.commandBar.placeholder')
            }
            aria-label={i18nT('apps.commandBar.title')}
            // Selection stays on the input and is announced through
            // aria-activedescendant, so arrow keys never move DOM focus off it.
            role="combobox"
            aria-expanded={rowCount > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={rowCount > 0 ? rowId(selected) : undefined}
            // The cue belongs on the active OPTION -- it says what Enter will do,
            // which a box round the field does not -- and this input is focused for
            // the whole life of the dialog, so an unconditional `focus-visible`
            // utility here renders a permanent box no launcher UI has. The residual
            // case is a list with no rows at all: `aria-activedescendant` is omitted
            // there, so no option exists to carry the cue and the field has to, or a
            // keyboard user sees nothing.
            //
            // Two things changed about that residual. It is now nearly unreachable --
            // every state that used to produce it (a fresh sessions view, a failed
            // search, a search that matched nothing) carries rows of its own, leaving
            // only an instance with no sessions whatsoever. And what it paints is a
            // neutral hairline rather than an accent ring: an accent-coloured box
            // around the one element that is ALWAYS focused read as the loudest thing
            // on a surface whose entire visual weight is supposed to sit on the
            // selected row.
            className={`flex-1 min-w-0 bg-transparent border-none outline-hidden rounded text-[13px] text-text placeholder:text-muted${
              rowCount === 0 ? ' focus-visible:ring-1 focus-visible:ring-border-strong' : ''
            }`}
          />
        </div>

        {/* No hand-off: the query typed into the bar above is unsaved — the
            navigation would close the bar and take it along. */}
        {actionError && (
          <div className="px-3 py-2 border-t border-border">
            <ErrorNotice message={actionError} variant="inline" />
          </div>
        )}

        {/* ARTIFACTS, FOLDERS AND CREWMATES: the sessions scope keeps the failure row
            it already had.
            The crewmates scope needs this MORE than the two beside it, and leaving it
            out was a real hole: its retry row deliberately carries no text because
            this notice is what names the failure, so without the disjunct a catalog
            rejection rendered one bare `Retry` and nothing else -- `findReport` never
            resolved, the endpoint, status and backend `code` were dropped, and a
            failed read was indistinguishable from a crew-less install. That is the
            exact pair of states `useAgents` records six triage passes being lost to.
            No hand-off: the combobox query is unsaved local state, and the
            hand-off navigation would unmount the command bar and discard it.
            Keep the notice outside the listbox so ErrorNotice can never put an
            interactive control inside an option; Retry remains a separate option
            on the combobox's Arrow/Enter path. */}
        {(artifactsFailed || foldersFailed || matesFailed) && (
          <div className="px-3 py-2 border-b border-border">
            <ErrorNotice
              message={searchFailedText}
              report={findReport(errMessage(searchError))}
              variant="inline"
            />
          </div>
        )}

        {/* FOLDERS: the shared settings read the order comes from has FAILED with no
            body to draw from, so the list below is the stored order whatever mode
            the person chose — said here the way the sidebar says it over its own
            tree (there is no sidebar on this surface), because a list drawn silently
            in the wrong order is the dead end, not the failure. The raw server
            string is the message so the notice's journal lookup still finds the
            endpoint and status -- under the title, smaller, because "config store"
            and "gateway" are the detail, not the lead; the plain line under it says
            what the list is showing (a picker: "listed", not the sidebar's
            "arrangement"), that nothing is asked of the reader (the shell's read
            retries on its own), and where the hand-off lives, since this notice has
            none: the query typed into the bar is unsaved and the navigation would
            take it along. */}
        {scope === 'folders' && folderSortError && (
          <div className="px-3 py-2 border-b border-border">
            <ErrorNotice
              variant="inline"
              className="flex-wrap"
              title={i18nT('pages.chatSidebar.folder_order_unavailable')}
              message={folderSortError}
              messagePlacement="below"
              testId="command-bar-folder-order-unavailable"
            />
            <p className="mt-0.5 text-[11px] text-muted" data-testid="command-bar-folder-order-unavailable-detail">
              {i18nT('pages.chatSidebar.folder_order_unavailable_detail_picker_ask')}
            </p>
          </div>
        )}

        <div className="overflow-y-auto py-1" id={listId} role="listbox" aria-label={i18nT('apps.commandBar.title')}>
          {rowCount === 0 ? (
            scopeLoading ? (
              // Placeholder rows rather than a centred "Searching…" line. The line
              // collapsed the panel to one text height and then jumped when results
              // landed; these hold roughly the space the rows will occupy, so
              // entering a view is one movement instead of two.
              <>
                <div aria-hidden className="px-3 py-1">
                  {SKELETON_WIDTHS.map((w, n) => (
                    <div key={n} className="flex items-center gap-2.5 py-2">
                      <span className="shrink-0 w-4 h-4 rounded bg-bg-hover" />
                      <span className="h-3 rounded bg-bg-hover" style={{ width: w }} />
                    </div>
                  ))}
                </div>
                {/* The placeholder bars are decoration and hidden from AT, so the
                    state they depict has to be said out loud somewhere. */}
                <span role="status" className="sr-only">
                  {i18nT('apps.commandBar.searching')}
                </span>
              </>
            ) : argCommand ? (
              // The argument state's body. It has no rows by design, so this is not an
              // empty state to apologise for -- it is the question, and the app's own
              // hint says what answers it.
              //
              // The PROMPT PREVIEW is the consent mechanism for `autoSend`. A
              // contributed command sends app-authored text to an agent with tools as
              // if the reader had typed it; the reader picked the row and supplied the
              // value, but had no way to see the instruction itself. Showing the
              // resolved text — with the value already spliced in — is what makes the
              // next Enter informed rather than merely deliberate. It appears only
              // once the value satisfies the pattern, so it always shows what would
              // actually be sent, never a half-built template.
              <div role="status" className="px-3 py-4 text-[12px] text-muted space-y-2">
                <p className="text-text">{argCommand.subtitle || argCommand.appLabel}</p>
                {/* Attribution, not decoration. `subtitle` falls back to the app label, so
                    an app that writes its own subtitle used to erase the only mention of
                    who authored the prompt -- and this is the step where that prompt is
                    about to go to an agent with tools. Rendered whenever the subtitle
                    displaced it, so provenance is never the thing that got overwritten. */}
                {argCommand.subtitle && <p className="text-[11px]">{argCommand.appLabel}</p>}
                {argCommand.argument?.hint && <p>{argCommand.argument.hint}</p>}
                {argCommand.autoSend && argumentIsValid(argCommand, query) && query.trim() && (
                  <div className="pt-1 space-y-1">
                    <p className="text-[11px] uppercase tracking-wide" id="cb-will-send">
                      {i18nT('apps.commandBar.will_send')}
                    </p>
                    <pre
                      ref={previewRef}
                      // A NAMED region, not a bare focusable block. The cue below tells the
                      // reader to scroll this box, and a scroll container is not reachable
                      // from the keyboard on its own -- Safari never focuses one
                      // implicitly -- so without `tabIndex` the consent surface instructs a
                      // keyboard-only reader to read a tail they cannot reach, and then
                      // Enter sends it. `role`/`aria-labelledby` are what make the stop
                      // legitimate rather than a tab stop on inert text: it borrows the
                      // "Will send" heading already above it, so a screen reader announces
                      // what the region is and no catalog gains a string.
                      role="region"
                      aria-labelledby="cb-will-send"
                      // A SCROLL CONTAINER is the case the a11y rule below cannot see: the
                      // box clips a 4000-character prompt and the cue underneath tells the
                      // reader to scroll it, so the tab stop is what makes that instruction
                      // followable without a mouse. The rule's own allowlist is `tabpanel`
                      // only, so a labelled `region` cannot satisfy it; suppressed on this
                      // line rather than widened globally, and paired with the role and
                      // name above so the stop announces itself rather than being a silent
                      // halt on inert text.
                      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
                      tabIndex={0}
                      className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded border border-border bg-bg-hover/40 p-2 text-[11px] text-text focus-ring-accent"
                    >
                      {resolvePrompt(argCommand, query)}
                    </pre>
                    {previewClipped && (
                      // The box is the consent, so it must not let the reader believe
                      // they have read an instruction that continues out of sight. A
                      // prompt may run to 4000 characters and the unscrolled tail is
                      // exactly where a misleading manifest would put the part it does
                      // not want read. Measured rather than guessed from a line count,
                      // because wrapping decides what actually overflows.
                      <p className="text-[11px] text-warn">
                        {i18nT('apps.commandBar.will_send_clipped')}
                      </p>
                    )}
                  </div>
                )}
              </div>
            ) : (
              // Every other state now carries rows of its own, so this is the one
              // case left: a corpus that is genuinely empty.
              //
              // In the artifacts view that is the ONLY way to reach this branch, and
              // the copy depends on it: a query that matched nothing pushes the
              // clear-query row, a failure pushes the retry row, and a load in
              // flight renders the skeleton above. So an empty list here means the
              // user has saved nothing yet, and the string says that instead of
              // reporting a failed match against a query they never typed.
              //
              // The folders view reaches it the same way and for the same reason, so
              // it gets the same treatment: a reader with no folders is told they
              // have none, not that no sessions matched.
              //
              // The crewmates view has NO branch here, deliberately. Every empty case it
              // can reach pushes a ROW instead — `open-crewmates` for no crew,
              // `clear-query-mates` for a query that matched nothing, `retry-mates` for a
              // failed read — so this centred notice is unreachable for that scope, and a
              // branch for it would be a sentence with no affordance that nothing renders.
              <div role="status" className="px-3 py-6 text-center text-[12px] text-muted">
                {scope === 'artifacts'
                  ? i18nT('apps.commandBar.no_artifacts_yet')
                  : scope === 'folders'
                    ? i18nT('apps.commandBar.no_folders_yet')
                    : scope
                      ? i18nT('apps.commandBar.no_sessions')
                      : i18nT('apps.commandBar.no_matches')}
              </div>
            )
          ) : (
            slots.map((slot, i) => {
              const header = headerOf(slot, slots[i - 1], query.trim().length > 0)
              return (
                <div key={slot.key}>
                  {header && (
                    <div className="px-3 pt-2 pb-0.5 text-[10px] uppercase tracking-wide text-muted">
                      {header}
                    </div>
                  )}
                  <div
                    id={rowId(i)}
                    role="option"
                    tabIndex={-1}
                    aria-selected={selected === i}
                    onMouseDown={() => activateIndex(i, 'pointer')}
                    onMouseMove={e => selectOnPointerMove(i, e)}
                    className={`flex items-center gap-2.5 px-3 py-2 cursor-pointer text-[13px] ${
                      selected === i ? 'bg-bg-hover text-text' : 'text-text'
                    }`}
                  >
                    {renderSlot(slot)}
                  </div>
                </div>
              )
            })
          )}
        </div>

        {/* What the row cap is hiding. Outside the listbox on purpose: it is a fact
            about the list, not a row in it, and as an option it would be the one
            Enter did nothing to. Only in the artifacts view, where the cap exists. */}
        {scope === 'artifacts' && artifactOverflow > 0 && (
          <div className="px-3 py-1.5 border-t border-border text-[11px] text-muted">
            {i18nT('apps.commandBar.artifacts_more_keep_typing', { count: artifactOverflow })}
          </div>
        )}

        {/* What Enter does, named. The bar's promise is that Enter does something
            specific to the highlighted row, and nothing said what: the row carried
            its TYPE ("Command", "App") while the verb was left to be inferred from
            having pressed Enter before. Rendered only when a row exists, because
            with none there is no action to name. Kept to one line of muted text and
            a keycap — the panel's weight belongs on the selected row. */}
        {rowCount > 0 && (
          <div className="flex items-center justify-end gap-2 px-3 py-1.5 border-t border-border text-[11px] text-muted">
            {copyNotice ? (
              /* The copy outcome takes the footer's OWN line rather than a strip of its
                 own. The panel is height-capped, so any strip added anywhere shortens
                 the list: a row at the bottom vanished on every copy and a reader could
                 not tell whether it had scrolled away or been pushed out. One line in,
                 one line out, and nothing else moves. Affordable because the outcome is
                 transient -- it clears on the next keystroke, which is exactly when the
                 Enter hint starts mattering again.

                 `role="status"` on the WRAPPER, so the address is announced together
                 with the word: a reader who cannot see the line still learns which of
                 two possible addresses landed. */
              <span role="status" className="flex items-center gap-1.5 min-w-0 flex-1">
                <span className={`shrink-0 ${copyNotice.what ? 'text-accent' : 'text-warn'}`}>
                  {copyNotice.text}
                </span>
                {copyNotice.what && (
                  <span className="min-w-0 truncate" title={copyNotice.what}>
                    {copyNotice.what}
                  </span>
                )}
              </span>
            ) : (
              <>
                <span className="truncate">{actionLabel(slots[Math.min(selected, rowCount - 1)])}</span>
                <span className="shrink-0 px-1 rounded border border-border leading-4">
                  {ENTER_KEY}
                </span>
                {/* The copy chord, named only while the selected row HAS an address. A
                    gesture with no visible counterpart is one most readers never learn
                    exists, and this footer is already where the surface says what a key
                    does to the highlighted row. Withheld rather than greyed on a row
                    with no address: the hint doubles as the answer to "can this one be
                    copied", which a permanently-present label could not give. */}
                {copyTarget && (
                  <>
                    <span className="shrink-0">{i18nT('apps.commandBar.copy_hint')}</span>
                    <span className="shrink-0 px-1 rounded border border-border leading-4">
                      {COPY_KEY}
                    </span>
                  </>
                )}
              </>
            )}
          </div>
        )}
        {/* BELOW the footer, not above the list, and that placement is the point: a
            notice mounted over the rows pushed the whole list down on every copy, and a
            reader watching the row they had just copied saw it move and could not tell
            whether it had scrolled away or gone. At the bottom of the panel a copy
            changes nothing about where anything else sits.

            A clipboard write that did not land is an ERROR, so it goes through the
            product's error surface rather than the status line below it.
            No hand-off: the query typed into the bar is unsaved -- the navigation would
            close the bar and take it along. */}
        {copyError && (
          <div className="px-3 py-2 border-t border-border">
            <ErrorNotice message={copyError.text} variant="inline" />
            {/* The address the write did not land, in FULL and selectable. The message
                above names a remedy -- select the text and copy it yourself -- and on
                this path nothing else on screen holds that text. Not truncated, for the
                same reason: a reader who selects half an address gets half a link. */}
            <div
              className="mt-1 text-[12px] text-muted select-all"
              style={{ overflowWrap: 'anywhere' }}
            >
              {copyError.what}
            </div>
          </div>
        )}

        {/* The argument state has no row to name an action for, and it is the state
            that most needs one: the verb here is "approve" or "merge", and it fires on
            the next Enter. The spinner lives here for the same reason -- the work is
            attached to the field rather than to a row, so there is nowhere else for it
            to appear, and without it a slow create reads as a dead keypress. */}
        {rowCount === 0 && argCommand && (
          <div className="flex items-center justify-end gap-2 px-3 py-1.5 border-t border-border text-[11px] text-muted">
            {pendingRow ? (
              <>
                <Loader2 size={12} className="lucide-inline animate-spin shrink-0" />
                <span className="truncate">{i18nT('apps.commandBar.working')}</span>
              </>
            ) : (
              <>
                <span className="truncate">{argCommand.title}</span>
                <span className="shrink-0 px-1 rounded border border-border leading-4">{ENTER_KEY}</span>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  </OverlayPortal>
}
