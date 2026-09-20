import { useEffect, useRef, useState } from 'react'
import { EyeOff, Loader, Pen, Sparkles, Undo2, VenetianMask } from 'lucide-react'
import { useImeGuard } from '../../hooks/useImeGuard'
import useHeldWindow from '../../hooks/useHeldWindow'
import { Btn, Input } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { MOVE_UNDO_MS } from '../../components/MoveUndoBar'
import TypewriterText from '../../components/TypewriterText'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { sseSlotTitle } from '../../store/dashboardSlice'
import { api } from '../../api/client'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'

/**
 * SessionTitleControl — the session title as ONE editable control: the
 * memory-mode glyph, the title (click / Enter to rename inline), the Pen that
 * reveals on `group/header` hover, and the Sparkles button that asks the LLM
 * for a new title (spinner while it runs).
 *
 * Shared by the single-session header (ChatPage) and every split-view pane
 * header (ChatPane) so the two cannot drift again — the pane header used to
 * render a bare title span with neither affordance (#9727). The HOST supplies
 * the `group/header` hover target; this control only renders the row.
 *
 * `editing` / `onEditingChange` are optional controlled state: ChatPage pins
 * its editor to the slot it opened on (`editingTitleSlot`) and opens it from
 * the header menu too, so it owns the flag; a pane leaves both unset and the
 * control keeps the flag itself.
 *
 * Failures are reported through `onError(message, title)`, never rendered
 * here: each host owns its own error surface (ChatPage's action banner, the
 * pane's inline ErrorNotice).
 */

/**
 * Per-slot recovery state for a refused rename (#10203, ported from the main
 * header so every host shares one failure semantics). `gen` is a monotonic
 * attempt generation: a recovery may apply ONLY while its own attempt is still
 * the slot's latest, so a delayed recovery can never overwrite anything a newer
 * attempt (failed or successful) did — title equality alone cannot tell a stale
 * optimistic value from a newer confirmed rename to the identical string.
 * `baseline` is the last CONFIRMED title, advanced only by a resolved rename
 * or a value the server re-read returned, and only when that confirmation is
 * newer (`confirmedGen`) than the last one applied, so a late-arriving older
 * success can never replace a newer confirmed write; `inflight` counts this slot's own
 * un-settled optimistic titles (a count, not a set: two overlapping attempts
 * at the identical string must both stay marked until each settles, or the
 * first to settle would unmark the second and a later commit could take that
 * still-optimistic value as a confirmed baseline), so a store title outside
 * that set refreshes the baseline at commit time (a success here, or another
 * client's rename delivered over SSE). The entry is dropped when the last
 * pending attempt settles.
 *
 * Module-level and keyed by slot, not per control instance: the main header
 * re-targets one instance across slots and split view mounts one per pane, and
 * the guard is about the SLOT, so every mount must see the same record.
 */
type RenameRecovery = { baseline: string; inflight: Map<string, number>; gen: number; confirmedGen: number }

/**
 * How long the Undo offer stays after a generated title lands: the same
 * horizon as the session-move undo bar, so the product has one undo window.
 */
const UNDO_WINDOW_MS = MOVE_UNDO_MS
// The rename route keeps `title.strip()[:200]` (chat_title.py). Without a cap
// the editor accepts a longer name, the optimistic write shows all of it, and
// the server's trailing cut only surfaces on a later re-read -- the typed tail
// is lost silently. Same figure as the task-run rename field (ProjectsPage).
// JS `maxLength` counts UTF-16 units and the server counts code points, so
// the client cap is never looser than the server's.
const TITLE_MAX_LENGTH = 200
const renameRecovery = new Map<string, RenameRecovery>()
export default function SessionTitleControl({
  slotKey,
  title,
  compact,
  editing: editingProp,
  onEditingChange,
  onError,
  onAttempt,
}: {
  slotKey: string
  title: string
  /** Pane typography (13px, strong text, smaller glyphs) instead of the main header's. */
  compact?: boolean
  editing?: boolean
  onEditingChange?: (editing: boolean) => void
  /** Rename / regenerate failure: `message` is the error text, `title` the i18n lead. */
  onError?: (message: string, title: string) => void
  /**
   * A new rename or regenerate is starting. A host that shows `onError` as a
   * persistent notice clears it here, so a stale "Couldn't rename" never sits
   * beside the spinner of the attempt that supersedes it.
   */
  onAttempt?: () => void
}) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const queryClient = useQueryClient()
  const memoryMode = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey)?.memory_mode)
  const [localEditing, setLocalEditing] = useState(false)
  const editing = editingProp ?? localEditing
  const setEditing = (next: boolean) => { setLocalEditing(next); onEditingChange?.(next) }
  // Leaving the slot abandons the draft (the editor below is mounted per edit,
  // so closing it drops the text). Mirrors ChatPage's `activeSlot` effect for
  // the uncontrolled case; a controlled host resets its own flag.
  useEffect(() => { setLocalEditing(false) }, [slotKey])
  // Keyed by slot, not a bare boolean: the main header re-targets this same
  // instance when the user switches sessions mid-generation, and the spinner
  // must follow the slot that is generating, not the one in front.
  const [generatingSlots, setGeneratingSlots] = useState<Set<string>>(new Set())
  const generating = generatingSlots.has(slotKey)
  // One-shot undo for a generated title. Auto-title replaces a name the user
  // may have typed with no confirm, and cold readers refused to press it for
  // exactly that reason (UX rounds); the previous name is kept here and
  // offered back inline until the user does something else with the title
  // (edit, another generate, a switch of slot) or the generated title is no
  // longer what the header shows. Keyed by slot like the spinner, so the main
  // header re-targeting to another session does not offer the wrong name back.
  const [undoable, setUndoable] = useState<{ slot: string; previous: string; next: string } | null>(null)
  const undo = undoable && undoable.slot === slotKey && undoable.next === title ? undoable : null
  // The title moving on ENDS the offer, it does not merely hide it: a title
  // that bounces away and back to the generated string (two remote renames
  // inside the window) must not bring a stale Undo back, because pressing it
  // would then write the pre-generation name over a rename this client never
  // offered to undo (GPT lane on `ee8eb9ab0`).
  useEffect(() => {
    if (undoable && undoable.slot === slotKey && undoable.next !== title) setUndoable(null)
  }, [undoable, slotKey, title])
  // Retargeting the control (the main header switching sessions) ends the
  // offer outright, and a generation that completes for a slot this instance
  // no longer shows must not create one: the offer is for the title the user
  // is looking at when it lands.
  const slotRef = useRef(slotKey)
  useEffect(() => { slotRef.current = slotKey; setUndoable(null) }, [slotKey])
  // The offer is a WINDOW, not a permanent state: while it stands it occupies
  // the Auto-title button's place (two-action row), so a user who wants to
  // re-roll a disliked name must otherwise Undo first. It closes on its own
  // after UNDO_WINDOW_MS (the session-move undo bar's horizon, so the product
  // has one undo window) and when the user opens the editor -- both are
  // "I have moved on" signals. (Re-rolling under a parked pointer, where the
  // window below is held open, is Undo then Auto-title: two clicks.)
  // Suspended while the pointer is over the row or focus is inside it -- the
  // session-move undo bar's rule, and its clock: `useHeldWindow` is the one
  // deadline/hold/remainder implementation both undo offers run on. The
  // tooltip that persuaded the user to press Auto-title promises the way back,
  // so the window must not run out under a hand that is still weighing the new
  // name (UX round on `05b6a996e`). The hold here is not keyed to the offer:
  // the pointer is on the button that was just pressed when the offer lands,
  // and the hook holds a FULL window for a fresh offer under a standing hold.
  // Pointer and focus are tracked apart and EITHER holds: one flag for both
  // would let a pointer leaving the row release a hold that keyboard focus
  // inside it still owns (GPT lane on `dc1ae57ac`).
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  useHeldWindow(undoable, UNDO_WINDOW_MS, hovered || focused, () => setUndoable(null))

  const report = (e: unknown, leadKey: string) =>
    onError?.(errMessage(e) || i18nT('pages.chatPage.unknown_error'), i18nT(leadKey))

  const commit = (draft: string) => {
    const refused = draft.trim()
    if (!refused || refused === title) return
    onAttempt?.()
    setUndoable(null)
    const key = slotKey
    const slotTitle = () => store.getState().dashboard.slots.find((s) => s.key === key)?.title
    const rec = renameRecovery.get(key) ?? { baseline: title, inflight: new Map<string, number>(), gen: 0, confirmedGen: 0 }
    const current = slotTitle() ?? title
    if (!rec.inflight.has(current)) rec.baseline = current
    rec.inflight.set(refused, (rec.inflight.get(refused) ?? 0) + 1)
    rec.gen++
    const myGen = rec.gen
    renameRecovery.set(key, rec)
    const settle = () => {
      const left = (rec.inflight.get(refused) ?? 1) - 1
      if (left > 0) rec.inflight.set(refused, left)
      else rec.inflight.delete(refused)
      if (rec.inflight.size === 0 && rec.gen === myGen) renameRecovery.delete(key)
    }
    // A recovery applies only while THIS attempt is the slot's latest AND the
    // store still shows its refused value — anything else means a newer write
    // (a later rename here, another client, a generated title) already landed.
    const mayRecover = () => rec.gen === myGen && slotTitle() === refused
    // Optimistic: the bar shows the new title at once.
    dispatch(sseSlotTitle({ key, title: refused }))
    api.renameSlot(key, refused).then(() => {
      // A confirmed write becomes the fallback baseline unless a NEWER attempt
      // was already confirmed (its response overtook this one): the server
      // holds the later write, so this one must not replace it. Gating on the
      // latest generation instead would drop a success that overlapped a later
      // attempt, and a failure of that later attempt would then fall back past
      // it to a title the server no longer holds.
      if (myGen > rec.confirmedGen) {
        rec.confirmedGen = myGen
        rec.baseline = refused
      }
      settle()
    }, async (e) => {
      // Report only while this attempt is still the slot's latest: a
      // superseded rename's late refusal is not news the user can act on
      // (they already renamed again), and surfacing it would re-raise the
      // notice a newer attempt's start just cleared. The recovery below still
      // runs, gated the same way, so the store is never left on a refused value.
      if (rec.gen === myGen) report(e, 'pages.chatPage.could_not_rename_session')
      // A refused rename must also revert the optimistic title. Re-read the
      // server truth (deduped through queryClient.fetchQuery so overlapping
      // failures share one request) and apply ONLY this slot's title — never
      // the whole snapshot, whose late fulfillment could clobber a newer
      // concurrent write of another slot. When the re-read fails too (a
      // transport or auth failure takes renameSlot and chatSlots down
      // together) fall back to a local revert to the recovery baseline.
      try {
        const server = (await queryClient.fetchQuery({
          queryKey: ['chat-slots'],
          queryFn: () => api.chatSlots(),
          staleTime: 0,
          gcTime: 0,
        })).find((s: { key: string; title?: string }) => s.key === key)
        if (server?.title !== undefined && myGen > rec.confirmedGen) {
          rec.confirmedGen = myGen
          rec.baseline = server.title
        }
        if (mayRecover()) dispatch(sseSlotTitle({ key, title: server?.title ?? rec.baseline }))
      } catch {
        if (mayRecover()) dispatch(sseSlotTitle({ key, title: rec.baseline }))
      } finally {
        settle()
      }
    })
  }

  const regenerate = () => {
    if (generating) return
    onAttempt?.()
    setUndoable(null)
    const slot = slotKey
    const slotTitleNow = () => store.getState().dashboard.slots.find((s) => s.key === slot)?.title ?? title
    const atClick = slotTitleNow()
    setGeneratingSlots((prev) => new Set(prev).add(slot))
    api.generateTitle(slot).then((r) => {
      /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */
      if (r.title) {
        // The Undo baseline is the title on screen at the moment the generated
        // one replaces it, so a rename that landed during the generation
        // (another pane, another client via SSE) is what Undo brings back.
        // The server also pushes the generated title over the slots stream,
        // and that push can arrive BEFORE this response: the store then
        // already shows r.title, which is not a baseline -- fall back to the
        // title at click time. What the server itself replaced is only
        // knowable server-side (#10149); this is the closest client truth.
        const landing = slotTitleNow()
        const previous = landing !== r.title ? landing : atClick
        dispatch(sseSlotTitle({ key: slot, title: r.title }))
        if (r.title !== previous && slotRef.current === slot) setUndoable({ slot, previous, next: r.title })
      }
    }).catch((e) => {
      report(e, 'pages.chatPage.could_not_generate_title')
    }).finally(() => setGeneratingSlots((prev) => { const next = new Set(prev); next.delete(slot); return next }))
  }

  const glyphs = (
    <>
      {memoryMode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
      {memoryMode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
    </>
  )

  if (editing) {
    return (
      <div className="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md bg-bg-hover">
        {glyphs}
        <TitleEditor
          initial={title}
          className={compact
            ? 'text-[13px] font-semibold text-text-strong font-body bg-transparent border-0 rounded-none p-0 m-0 min-w-0 flex-1 outline-hidden focus:!shadow-none focus-visible:border-b focus-visible:border-accent'
            : 'session-header-title text-sm font-semibold text-muted font-body bg-transparent border-0 rounded-none p-0 m-0 min-w-0 flex-1 outline-hidden md:max-w-[50vw] focus:!shadow-none focus-visible:border-b focus-visible:border-accent'}
          onCommit={commit}
          onClose={() => setEditing(false)}
        />
      </div>
    )
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- the handlers hold the Undo window open and perform no action a role could announce; the pointer pair is the hover hold, the focus pair its keyboard parity (any focus inside the row holds the same clock), as on the session-move undo bar
    <div
      className="cursor-text flex min-w-0 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover focus-within:bg-bg-hover transition-colors"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={() => setFocused(false)}
    >
      {/* Keyboard parity with hover: the Pen reveals when the title has
          focus-visible, the Auto-title button when it does -- a keyboard user
          tabbing through the header otherwise gets a focus ring around
          nothing. Same one-line rule for both hosts (UX round). */}
      <Clickable className="group/title flex min-w-0 items-center gap-1" onClick={() => { if (generating) return; setUndoable(null); setEditing(true) }}>
        {glyphs}
        <TypewriterText
          text={title}
          className={compact
            ? 'text-[13px] font-semibold text-text-strong font-body truncate min-w-0'
            : 'session-header-title text-sm font-semibold text-muted font-body truncate min-w-0 md:max-w-[50vw]'}
        />
        <Pen size={compact ? 12 : 13} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-60 group-focus-visible/title:opacity-60 transition-opacity" />
      </Clickable>
      {undo
        ? (
          /* While the offer stands it takes the Auto-title button's place, so
             the row never holds more than two actions (rename + one of
             Auto-title / Undo). Always visible (not hover-gated) while the
             generated title is the one on screen: the way back must be seen
             without knowing to hover. */
          <Btn
            aria-label={`${i18nT('pages.chatPage.undo_auto_title')}: ${undo.previous}`}
            title={undo.previous}
            className={`shrink-0 flex items-center gap-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 ${compact ? 'text-[11px]' : 'text-xs'} leading-none whitespace-nowrap`}
            onClick={(e) => { e.stopPropagation(); commit(undo.previous) }}
          >
            <Undo2 size={compact ? 12 : 13} />
            {i18nT('pages.chatPage.undo_auto_title')}
          </Btn>
        )
        : generating
        ? (
          /* Busy-naming is NAMED: the spinner keeps the action's label beside
             it (with an ellipsis), so the swap from the Auto-title button to a
             bare glyph does not read as "something unknown is happening" --
             and the status is announced, not just drawn. */
          <span role="status" aria-live="polite" className="shrink-0 flex items-center gap-1 text-accent">
            <Loader size={compact ? 14 : 16} className="shrink-0 animate-spin" />
            <span className={compact ? 'text-[11px] leading-none whitespace-nowrap' : 'text-xs leading-none whitespace-nowrap'}>{i18nT('pages.chatPage.auto_title')}…</span>
          </span>
        )
        : (
          <Btn
            aria-label={i18nT('pages.chatPage.regenerate_title_with_llm')}
            title={i18nT('pages.chatPage.regenerate_title_with_llm')}
            className="shrink-0 flex items-center gap-1 text-muted opacity-0 group-hover/header:opacity-70 focus-visible:opacity-100 focus-visible:text-accent hover:!opacity-100 hover:text-accent transition-all cursor-pointer bg-transparent border-none p-0"
            onClick={(e) => { e.stopPropagation(); regenerate() }}
          >
            <Sparkles size={compact ? 14 : 16} />
            {/* The glyph alone reads as an unexplained sparkle next to the
                composer's; the hover-revealed state names the action. Revealed
                at 70%, not the Pen's 60%/40%: with a TEXT label beside it, the
                dimmer value read as "disabled" to every cold reader (UX rounds);
                muted colour still marks it secondary, full opacity on hover. */}
            <span className={compact ? 'text-[11px] leading-none whitespace-nowrap' : 'text-xs leading-none whitespace-nowrap'}>{i18nT('pages.chatPage.auto_title')}</span>
          </Btn>
        )}
    </div>
  )
}

/**
 * The inline editor, mounted per edit so mounting IS seeding: the draft starts
 * from the title the editor opened on and dies with it, so no stale draft can
 * survive a close and commit against a title that changed in the meantime.
 */
function TitleEditor({ initial, className, onCommit, onClose }: {
  initial: string
  className: string
  onCommit: (draft: string) => void
  onClose: () => void
}) {
  // Both seeded ONCE at mount: `initial` is the live title prop and moves when
  // a remote / generated rename lands while the editor is open, but the seed
  // this draft started from must not.
  const [seed] = useState(initial)
  const [draft, setDraft] = useState(initial)
  // Escape closes without committing. The flag (not just the close) is needed
  // because the browser may still fire blur on the unmounting input.
  const cancelRef = useRef(false)
  // Enter-to-commit input: the guard owns both the composition latch and the
  // keypress, so the rename cannot fire on the Enter that commits an IME candidate.
  const ime = useImeGuard()
  return (
    <Input
      className={className}
      size={Math.min(Math.max(draft.length + 2, 6), 80)}
      autoFocus
      maxLength={TITLE_MAX_LENGTH}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      {...ime.bindComposition<HTMLInputElement>({
        // Select the whole title on open, anchored BACKWARD. Caret-at-end
        // autofocus scrolls a long title so only its tail shows, which reads
        // as "part of the name is already gone" in a narrow pane. A plain
        // select() keeps the selection focus at the end, so the browser still
        // scrolls to the tail; with the focus at the start the input scrolls
        // to 0 and the beginning of the name is what shows. Retype and
        // append-after-End stay one keystroke each. Routed through the IME
        // guard, whose spread owns the input's focus handler.
        onFocus: (e) => {
          const el = e.currentTarget
          el.setSelectionRange(0, el.value.length, 'backward')
          el.scrollLeft = 0
        },
        onBlur: () => {
          // An untouched editor writes nothing: it was seeded from the title
          // at open, and the live title may have moved on since (a generated
          // or remote rename), so committing the seed would overwrite that.
          if (!cancelRef.current && draft.trim() !== seed.trim()) onCommit(draft)
          cancelRef.current = false
          onClose()
        },
      })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && ime.claimEnter(e)) (e.target as HTMLInputElement).blur()
        if (e.key === 'Escape') { ime.reset(); cancelRef.current = true; onClose() }
      }}
    />
  )
}
