/** The crew editor's unsaved-work guard: which panes hold an edit not yet
 *  saved (`dirtyPanes`), the rail pane on show, and the one discard question
 *  every destructive exit asks (a pane switch away from a schedule draft, a
 *  dismissal, the chat jump, the wake section's own cancel), with the dialog
 *  that asks it. */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Btn } from '../components/ui'
import {
  Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import { ghostTraitsFrom, iconAvatarFrom, imageAvatarFrom, packAvatarFrom, unclaimedAvatarFrom, type CrewAvatarOverride } from '../components/CrewAvatar'
import {
  motionsFrom,
  soundsFrom,
} from '../lib/crewAvatarState'
import { type CrewPaneKey } from '../components/crew/crewEditorSections'
import { type KiroCrewAgent } from '../components/AgentSelector'
import { i18nT } from '../i18n/t'
import { INHERIT_MODEL } from './KiroCrewAgentsPageFields'

/** Which crew the editor dialog is pointed at. `null` = closed. */
export type SheetTarget = { mode: 'edit'; name: string } | null

/** How long a schedule-draft discard confirm stays fully locked while the
 *  create request is in flight. The lock exists because discarding cannot
 *  cancel the POST; the unlock exists because a HUNG request (the client
 *  sets no timeout) must not seal every exit from the modal editor. A
 *  healthy create settles well under this, so the escape only surfaces for
 *  genuinely stalled requests. */
const DISCARD_FORCE_GRACE_MS = 8000

/** The editor's discard confirm, nested inside the editor's DialogContent. */
export function CrewDiscardDialog({ discardAsk, setDiscardAsk, askSchedOnly, schedDraft, discardTakesSheet, schedSaving, discardForce, editing, confirmDiscard }: {
  discardAsk: CrewPaneKey | 'close' | 'chat' | 'collapse' | null
  setDiscardAsk: (next: CrewPaneKey | 'close' | 'chat' | 'collapse' | null) => void
  askSchedOnly: boolean
  schedDraft: boolean
  discardTakesSheet: boolean
  schedSaving: boolean
  discardForce: boolean
  editing: string
  confirmDiscard: () => void
}) {
  return (
    <Dialog open={discardAsk !== null} onOpenChange={next => { if (!next) setDiscardAsk(null) }}>
      <DialogContent
        maxWidth={440}
        className="z-[110]"
        aria-label={askSchedOnly
          ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
          : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
      >
        <DialogHeader>
          {/* The shared title truncates by default; at 320px that
              clipped "Discard unsaved chang…", so this one wraps. */}
          <DialogTitle className="whitespace-normal">
            {askSchedOnly
              ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
              : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
          </DialogTitle>
        </DialogHeader>
        <DialogBody>
          <p className="m-0 text-sm text-text">
            {schedDraft
              ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule_body')
              : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_body', { name: editing })}
          </p>
          {/* Full contrast, same as the line above: this is the half of
              the consequence the narrow question left out, so it must not
              read as a footnote to it. */}
          {schedDraft && discardTakesSheet && (
            <p className="mb-0 mt-2 text-sm text-text" data-testid="crew-sched-discard-also-crew">
              {i18nT('pages.kiroCrewAgentsPage.discard_also_crew_edits')}
            </p>
          )}
          {/* The reason Discard is locked, as VISIBLE text: the button's
              `title` never reaches keyboard or touch users, and browsers
              often suppress titles on disabled controls entirely. */}
          {schedSaving && (
            <p className="mb-0 mt-2 text-[12px] text-muted" data-testid="crew-sched-discard-saving-note">
              {discardForce
                ? i18nT('pages.kiroCrewAgentsPage.discard_anyway_note')
                : i18nT('pages.kiroCrewAgentsPage.discard_locked_while_saving')}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Btn onClick={() => setDiscardAsk(null)} data-testid="crew-sched-discard-keep">
            {i18nT('pages.kiroCrewAgentsPage.keep_editing')}
          </Btn>
          <Btn
            danger
            onClick={confirmDiscard}
            // While the create request is in flight, discarding would not
            // cancel it -- the schedule would persist after the user
            // watched it "discarded". Locked until the request settles,
            // EXCEPT after the grace period: a hung request must not
            // seal every exit from the editor, so the button unlocks
            // and the visible note carries the may-still-persist caveat.
            disabled={schedSaving && !discardForce}
            title={schedSaving ? i18nT('components.jobForm.saving') : undefined}
            data-testid="crew-sched-discard-confirm"
          >
            {askSchedOnly
              ? i18nT('pages.kiroCrewAgentsPage.discard_schedule_confirm')
              : i18nT('pages.kiroCrewAgentsPage.discard_confirm')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The editor's pane, its dirty panes and every guarded exit, called where
 *  they sat in the page so their hooks register in the page's order. */
export function useCrewDiscardGuard({
  sheet,
  editing,
  editingAgent,
  kiroAgent,
  workspace,
  memoryStore,
  editModel,
  editEffort,
  triggers,
  displayName,
  sessionColor,
  editAvatar,
  capabilityDirty,
  capabilityBusy,
  committing,
  linkedPaneRef,
  discardAnswer,
  templateSwitchInflight,
  instantSaveInflight,
  closeSheet,
  settleDiscardAnswer,
  chatWith,
}: {
  sheet: SheetTarget
  editing: string
  editingAgent: KiroCrewAgent | undefined
  kiroAgent: string
  workspace: string
  memoryStore: string
  editModel: string
  editEffort: string
  triggers: string
  displayName: string
  sessionColor: string
  editAvatar: CrewAvatarOverride | null
  capabilityDirty: boolean
  capabilityBusy: boolean
  committing: boolean
  linkedPaneRef: React.MutableRefObject<CrewPaneKey | null>
  discardAnswer: React.MutableRefObject<{ answered: Promise<boolean>; settle: (discarded: boolean) => void } | null>
  templateSwitchInflight: React.MutableRefObject<Promise<void> | null>
  instantSaveInflight: React.MutableRefObject<Promise<unknown> | null>
  closeSheet: () => void
  settleDiscardAnswer: (discarded: boolean) => void
  chatWith: (crew: string) => Promise<void>
}) {
  /** Which rail pane the editor body is showing. Reset whenever the editor is
   *  pointed somewhere else, so a crew never opens on the pane the previous one
   *  happened to be left on. */
  const [pane, setPane] = useState<CrewPaneKey>('overview')
  const [schedDraft, setSchedDraft] = useState(false)
  /** True while the schedule draft's create request is in flight. Discarding
   *  then would unmount the form WITHOUT cancelling the POST, so the schedule
   *  the user watched being "discarded" persists — the confirm's destructive
   *  button locks on this for exactly the reason the pane's own toggle does. */
  const [schedSaving, setSchedSaving] = useState(false)
  /** Where the schedule-draft discard confirm would go if confirmed: a pane
   *  key to switch to, 'close' to dismiss the editor, or 'chat' to open a
   *  chat with this crew. null = no confirm showing. One state drives EVERY
   *  destruction path so they get the same guard for the same reason -- the
   *  footer's Save is already disabled for this draft, and a rail click, an
   *  Escape, or the header's chat jump destroying it silently would make the
   *  one tracked-dirty pane the one pane whose work a click erases. */
  const [discardAsk, setDiscardAsk] = useState<CrewPaneKey | 'close' | 'chat' | 'collapse' | null>(null)
  /** Escape hatch for a hung save: the create POST has no client timeout, so
   *  a stalled request would otherwise lock EVERY exit from the editor for
   *  as long as it stalls. After a grace period with the confirm open and
   *  the save still in flight, the destructive button unlocks — the note
   *  then says the honest thing: discarding cannot cancel the POST, so the
   *  schedule may still be created. */
  const [discardForce, setDiscardForce] = useState(false)
  useEffect(() => {
    if (discardAsk === null || !schedSaving) { setDiscardForce(false); return }
    const t = setTimeout(() => setDiscardForce(true), DISCARD_FORCE_GRACE_MS)
    return () => clearTimeout(t)
  }, [discardAsk, schedSaving])
  /** The section-owned collapse to run if the user confirms discarding via
   *  the toggle ('collapse' target). A ref, not state: it is a continuation,
   *  not something the render reads. */
  const collapseProceed = useRef<(() => void) | null>(null)
  useEffect(() => {
    // The linked pane is consumed only by the opening that shows it: with cached
    // agent data the link effect can run before `sheet` is set, and a null-sheet
    // pass here would otherwise spend the ref on nothing and land on Overview.
    if (sheet) { setPane(linkedPaneRef.current ?? 'overview'); linkedPaneRef.current = null } else setPane('overview')
    setSchedDraft(false); setSchedSaving(false); setDiscardAsk(null)
  }, [sheet, linkedPaneRef])

  /** Which panes hold an edit not yet saved. Compared against the SAVED crew,
   *  so a value the user typed and then typed back is not reported as pending.
   *  Computed ahead of the guards below because they key on it: what the
   *  dismissal question protects IS this set. */
  const dirtyPanes = useMemo(() => {
    const out = new Set<CrewPaneKey>()
    if (!editingAgent) return out
    if (kiroAgent !== (editingAgent.kiro_agent || '')) out.add('template')
    if (workspace !== (editingAgent.workspace || '') || memoryStore !== (editingAgent.memory_store || '')) {
      out.add('place')
    }
    if (editModel !== (editingAgent.model || INHERIT_MODEL)) out.add('model')
    if (editEffort !== (editingAgent.reasoning_effort || '')) out.add('model')
    if (triggers !== (editingAgent.triggers || '')) out.add('routing')
    // Trim-compared on BOTH sides, because the server trims on save: "  Ops  "
    // over a stored "Ops" persists nothing, so it must not read as an unsaved
    // change — and a hand-edited config holding "  Ops  " must not open dirty.
    if (displayName.trim() !== (editingAgent.display_name || '').trim()) out.add('routing')
    if (sessionColor !== (editingAgent.session_color || '')) out.add('routing')
    // Every tier in one comparison: ghost traits normalize through
    // ghostTraitsFrom (flat record, stable key order), an image override through
    // imageAvatarFrom and a pack through packAvatarFrom — so a picture pick, a
    // replace, a removal or a pack swap is dirty exactly like a trait edit.
    // pendingData is part of the draft's identity on purpose: a newly chosen
    // picture IS an unsaved change.
    const savedNorm =
      ghostTraitsFrom(editingAgent.avatar) ??
      iconAvatarFrom(editingAgent.avatar) ??
      imageAvatarFrom(editingAgent.avatar) ??
      packAvatarFrom(editingAgent.avatar)
    const draftNorm =
      editAvatar?.kind === 'ghost'
        ? (editAvatar.traits ?? null)
        : editAvatar?.kind === 'icon'
          ? { pose: editAvatar.pose, bg: editAvatar.bg }
          : editAvatar?.kind === 'image'
            ? { v: editAvatar.v, pendingData: editAvatar.pendingData }
            : editAvatar?.kind === 'pack'
              ? { id: editAvatar.id }
              : null
    if (JSON.stringify(draftNorm) !== JSON.stringify(savedNorm)) out.add('routing')
    // Compared separately from the face, and BOTH sides go through the same
    // coercion — which is what makes the comparison sound rather than merely
    // convenient. `JSON.stringify` is order-sensitive: the draft's map is in
    // the order the user touched the states, while the coercion always emits
    // them in its own fixed order. Comparing the raw draft against a coerced
    // record would report two identical sets of reactions as a change, and the
    // rail would show an unsaved dot on a freshly saved crew.
    //
    // A record no reader claimed is held WHOLE in the passthrough by `openEdit`,
    // which seeds NO draft for it. Reading its reactions on the saved side would
    // then compare a map against nothing and put an unsaved dot on a crew that
    // was only just opened, so the saved side reads as "no draft either", which
    // is what it is.
    const unclaimed = unclaimedAvatarFrom(editingAgent.avatar) !== null
    const savedReactions = unclaimed
      ? [null, null]
      : [motionsFrom(editingAgent.avatar), soundsFrom(editingAgent.avatar)]
    const draftReactions = [motionsFrom(editAvatar), soundsFrom(editAvatar)]
    if (JSON.stringify(draftReactions) !== JSON.stringify(savedReactions)) out.add('routing')
    // An open inline schedule-create form is pending work too: it gets the
    // rail's unsaved dot and the note, so closing the editor cannot silently
    // eat a half-typed schedule the way an untracked surface would.
    if (schedDraft) out.add('schedules')
    if (capabilityDirty) out.add('capabilities')
    return out
  }, [editingAgent, kiroAgent, workspace, memoryStore, editModel, editEffort, triggers, displayName, sessionColor, schedDraft, editAvatar, capabilityDirty])

  /** Rail-driven pane changes route through here: leaving the schedules pane
   *  while a schedule draft is open asks before destroying the typed work
   *  (the form's state is component-local and unmounts with the pane). The
   *  other panes' edits live in this component's state and survive a pane
   *  switch, so only the draft is at stake here. */
  const requestPane = useCallback((key: CrewPaneKey) => {
    if (schedDraft && key !== pane) { setDiscardAsk(key); return }
    setPane(key)
  }, [schedDraft, pane])

  /**
   * Editor dismissal (footer Cancel, Escape, overlay click) routes through
   * here: it asks before throwing away ANY unsaved pane edit, not just a typed
   * schedule.
   *
   * An open schedule draft is tested FIRST. `dirtyPanes` contains 'schedules'
   * while the draft is open, so that order is what decides which question the
   * user gets, and the draft's leg is not interchangeable with the generic one:
   * it locks its destructive button while the draft's create POST is in flight
   * (discarding cannot cancel the request) and unlocks it after a grace period,
   * neither of which the generic question needs. It does have to WIDEN what it
   * claims when other panes are dirty too — see `discardTakesSheet`.
   *
   * A COMMITTING write is dismissed with no question at all: the values on
   * screen are the ones the user just submitted, so nothing there is unsaved,
   * and the request is not cancellable — offering to discard would promise a
   * rollback the backend will not honor and the edits would land anyway.
   * Dismissing mid-write stays allowed; sheetEpoch/settleFor is what makes the
   * abandoned write land harmlessly on the UI. The create form is not tracked
   * by `dirtyPanes` (same `!creating` scoping as the footer's unsaved note), so
   * it closes immediately too.
   */
  const requestClose = useCallback(() => {
    if (capabilityBusy) return
    if (schedDraft) { setDiscardAsk('close'); return }
    // A template switch write still in the air cannot be discarded — the
    // request is already sent. Hold the close until it settles (the tracked
    // promise also awaits the roster refetch), then re-evaluate with fresh
    // dirty state: a landed switch is no longer dirty, so the discard dialog
    // never gets to promise an undo it cannot deliver.
    if (templateSwitchInflight.current) {
      void templateSwitchInflight.current.then(() => requestCloseRef.current())
      return
    }
    // Same hold for the pane's instant saves (model pick, skill toggle): the
    // PATCH is already sent, so the close waits for it to settle — a failure
    // then renders in the still-mounted pane instead of nowhere.
    if (instantSaveInflight.current) {
      void instantSaveInflight.current.catch(() => undefined).then(() => requestCloseRef.current())
      return
    }
    if (committing || dirtyPanes.size === 0) { closeSheet(); return }
    // Published BEFORE the question goes up so a save still staging an upload
    // sees it and holds its PUT until the answer arrives. Only this leg arms
    // it: the schedule draft disables Save, so no staging save can be in
    // flight behind the draft's own question.
    let settle: (discarded: boolean) => void = () => {}
    const answered = new Promise<boolean>(resolve => { settle = resolve })
    discardAnswer.current = { answered, settle }
    setDiscardAsk('close')
  }, [schedDraft, committing, dirtyPanes, closeSheet, capabilityBusy, templateSwitchInflight, instantSaveInflight, discardAnswer])

  /** Latest requestClose, for the deferred re-invocation above — the settle
   *  callback must not capture a stale closure's dirty state. */
  const requestCloseRef = useRef<() => void>(() => {})
  useEffect(() => { requestCloseRef.current = requestClose }, [requestClose])

  /** The header's "Chat with this crew" routes through here: it creates a
   *  chat slot, closes the sheet and navigates -- three steps that would
   *  destroy an open schedule draft as silently as an unguarded Escape. */
  const requestChat = useCallback(() => {
    if (capabilityBusy) return
    if (schedDraft || capabilityDirty) { setDiscardAsk('chat'); return }
    void chatWith(editing)
  }, [schedDraft, editing, capabilityDirty, capabilityBusy]) // eslint-disable-line react-hooks/exhaustive-deps -- chatWith is re-created per render; depping it would make this callback churn for no behavioural gain

  /** The wake section's own cancel toggle asks here before collapsing a
   *  dirty draft -- the one destruction path the page cannot intercept
   *  itself (at narrow widths it is a bare icon-only X). */
  const requestCancelDraft = useCallback((proceed: () => void) => {
    collapseProceed.current = proceed
    setDiscardAsk('collapse')
  }, [])

  const confirmDiscard = useCallback(() => {
    const target = discardAsk
    setDiscardAsk(null)
    // Tell a save that is waiting on this question that its edits are being
    // thrown away, so it returns instead of committing them.
    settleDiscardAnswer(true)
    // The form unmounts with the pane or the sheet; its unmount cleanup is
    // what clears `schedDraft`, so nothing here resets the flag by hand. The
    // chat path only destroys on SUCCESS: a failed slot-create keeps the
    // sheet open (chatWith settles the error and returns), draft intact.
    if (target === 'close') closeSheet()
    else if (target === 'chat') void chatWith(editing)
    else if (target === 'collapse') { collapseProceed.current?.(); collapseProceed.current = null }
    else if (target) setPane(target)
  }, [discardAsk, closeSheet, editing, settleDiscardAnswer]) // eslint-disable-line react-hooks/exhaustive-deps -- same chatWith identity note as requestChat

  /** Every exit from the question that is NOT a confirm answers it "keep": the
   *  Keep-editing button, an Escape on the confirm, and the sheet-change reset
   *  all land here. A save holding its PUT is released rather than killed —
   *  the user asked for that save and then chose to stay. */
  useEffect(() => {
    if (discardAsk === null) settleDiscardAnswer(false)
  }, [discardAsk, settleDiscardAnswer])

  /**
   * Whether the pending question destroys the WHOLE editor while panes OTHER
   * than the schedule draft are dirty — the case where the draft's narrow
   * question has to name them.
   *
   * 'close' and 'chat' both end in closeSheet, so answering "Discard schedule"
   * over a dialog that mentioned only the typed schedule is how an
   * untouched-looking Model or Triggers edit disappears without ever being
   * asked about. A pane switch and a form collapse keep the sheet, so the
   * narrow question is the true one there and neither escalates.
   */
  const discardTakesSheet =
    (discardAsk === 'close' || discardAsk === 'chat')
    && [...dirtyPanes].some(k => k !== 'schedules')

  /** The question stays the NARROW schedule one only while a draft is the
   *  single thing at stake: with other panes going too it must name them, and
   *  with no draft open it was never about a schedule. */
  const askSchedOnly = schedDraft && !discardTakesSheet

  /** Pane changes driven from INSIDE a pane (an overview diagram node) rather
   *  than from the rail. The clicked node unmounts with its pane, which would
   *  drop keyboard focus to the body — so focus moves to the arriving panel,
   *  which carries `tabIndex={-1}` for exactly this hand-off. Rail clicks keep
   *  focus on the rail row and never set this flag. */
  const paneFocusPending = useRef(false)
  const goToPane = useCallback((key: CrewPaneKey) => {
    setPane(prev => {
      // Arm only on a real change: a same-pane call never reruns the focus
      // effect, so an armed flag would fire on the NEXT rail-driven change and
      // steal focus the rail contract says stays on the rail row. The ref
      // write is idempotent, so a double-invoked updater is harmless.
      if (prev !== key) paneFocusPending.current = true
      return key
    })
  }, [])
  const panelId = `crew-editor-pane-${editing || 'new'}`
  useEffect(() => {
    if (!paneFocusPending.current) return
    paneFocusPending.current = false
    document.getElementById(`${panelId}-${pane}`)?.focus()
  }, [pane, panelId])
  return {
    pane,
    schedDraft,
    setSchedDraft,
    schedSaving,
    setSchedSaving,
    discardAsk,
    setDiscardAsk,
    discardForce,
    dirtyPanes,
    requestPane,
    requestClose,
    requestChat,
    requestCancelDraft,
    confirmDiscard,
    discardTakesSheet,
    askSchedOnly,
    goToPane,
    panelId,
  }
}
