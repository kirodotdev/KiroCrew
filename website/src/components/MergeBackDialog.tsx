import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { AlertCircle, AlertTriangle, GitMerge, Info, Loader2, RotateCw } from 'lucide-react'
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { api } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { resumeFromHistory, switchSlot } from '../store/chatSlice'
import { useMergeBackDialog } from '../hooks/useMergeBackDialog'
import { mergeBackErrorCode, mergeBackErrorMessage } from '../utils/mergeBackError'
import { reportForError, type ErrorReport } from '../utils/errorReport'
import { focusComposer } from '../pages/chat/composerFocus'
import { i18nT } from '../i18n/t'
import type { MergeBackDraft } from '../types/mergeBack'

/** The gateway's limit (`MAX_MERGE_SUMMARY_CHARS`, the deferred-note bound). */
export const MAX_MERGE_SUMMARY_CHARS = 4000

/** Characters the way the gateway counts them: code points, not UTF-16 units. */
export function charCount(text: string): number {
  return Array.from(text).length
}

/** *text* cut to *room* characters at a word boundary, as the gateway cuts a long summary. */
function fitTo(text: string, room: number): string {
  const chars = Array.from(text)
  if (chars.length <= room) return text
  let cut = chars.slice(0, Math.max(room - 1, 0)).join('')
  const boundary = Math.max(cut.lastIndexOf('\n'), cut.lastIndexOf(' '))
  if (boundary > room / 2) cut = cut.slice(0, boundary)
  return cut.trimEnd() + '…'
}

/** Characters the summary may take once the gap sentence has its room. */
function summaryRoom(): number {
  const gap = i18nT('components.mergeBackDialog.gap_sentence')
  // The gateway cuts the summary to the whole limit, so the sentence's room comes
  // out of the summary: the text has to open within what Merge accepts.
  return MAX_MERGE_SUMMARY_CHARS - charCount(gap) - 2
}

/**
 * The text a merge starts from: the drafted summary, then one sentence saying
 * that the fork did not see anything the parent received after it was made. The
 * sentence is part of the text, not a label beside it, because the text is all
 * the parent's agent receives. It is always there and names no count: it is true
 * whether or not the parent moved on, so it can never go stale while the dialog
 * is open and the merge never has to check it.
 */
export function initialMergeText(draft: Pick<MergeBackDraft, 'summary'>): string {
  const gap = i18nT('components.mergeBackDialog.gap_sentence')
  const fitted = fitTo(draft.summary.trim(), summaryRoom())
  return [fitted, gap].filter(Boolean).join('\n\n')
}

/**
 * Whether the text `initialMergeText` opens with lost the end of the summary:
 * the gateway cut the model's note to the limit, or the summary had no room
 * left for the gap sentence. Either way the person is merging less than the
 * model wrote, and nothing in the text itself says so.
 */
export function draftWasCut(draft: Pick<MergeBackDraft, 'summary' | 'trimmed'>): boolean {
  return draft.trimmed || charCount(draft.summary.trim()) > summaryRoom()
}

/**
 * Merge into parent. Mounted once by ChatPage and opened through
 * `useMergeBackDialog`.
 *
 * Opening it drafts a summary of the fork messages the parent does not have
 * yet. The person reads and edits it; Merge writes exactly that text into the
 * parent as a card, which the parent's agent gets on its next turn. Nothing is
 * written until Merge, so closing the dialog at any point leaves both chats as
 * they were; closing over edited text asks first, since the text itself is the
 * one thing a close does lose.
 */
export default function MergeBackDialog() {
  const dialog = useMergeBackDialog()
  const forkKey = dialog?.forkKey ?? null
  if (!forkKey) return null
  // A fresh form per fork, so reopening for another fork starts clean.
  return <MergeBackForm key={forkKey} forkKey={forkKey} onClose={() => dialog?.close()} />
}

type Phase =
  | { kind: 'drafting' }
  | { kind: 'ready'; draft: MergeBackDraft }
  // `report` is the failed request's own record: the message is reworded into
  // the user's language, so the hand-off cannot find the record by its text.
  | { kind: 'refused'; code: string | undefined; message: string; report?: ErrorReport }
  // A draft refusal that describes where the chats stand: the parent already
  // has everything. Nothing failed, so there is no record to hand to the agent.
  | { kind: 'state'; code: StateCode; message: string }
  | { kind: 'merging'; draft: MergeBackDraft }
  | { kind: 'held'; parent: string }

/**
 * Merge refusals after which this draft cannot be merged: it no longer describes
 * the fork, or the parent it was drafted for was deleted or cannot be confirmed
 * as the parent. The form and the person's text stay, so nothing they wrote is
 * lost, but Merge stays off until a new draft arrives: the same draft could only
 * be refused again. "Draft again" over edited text then offers to keep that text
 * under the new draft, so the edits need not be copied out and typed back.
 */
const DRAFT_OUTDATED = new Set(['already_merged', 'merge_point_missing', 'merge_draft_stale', 'nothing_to_merge', 'parent_deleted', 'parent_unconfirmed'])

/** Draft refusals that drafting again would only repeat, so the dialog offers no retry. */
const DRAFT_FINAL = new Set(['nothing_to_merge', 'parent_deleted', 'parent_unconfirmed'])

/**
 * `nothing_to_merge` means the parent already has every fork message. It is an
 * empty state, which `errors-use-error-notice` excludes, so it stays in the muted
 * register instead of becoming a failed-request notice.
 */
type StateCode = 'nothing_to_merge'
const REFUSAL_IS_A_STATE: ReadonlySet<string> = new Set<StateCode>(['nothing_to_merge'])

function stateCodeOf(code: string | undefined): StateCode | null {
  return code && REFUSAL_IS_A_STATE.has(code) ? (code as StateCode) : null
}

function MergeBackForm({ forkKey, onClose }: { forkKey: string; onClose: () => void }) {
  const dispatch = useAppDispatch()
  const fork = useAppSelector(s => s.dashboard.slots.find(x => x.key === forkKey))
  const parentSession = fork?.forked_from || ''
  // The parent's slot key: the draft names it, and before that the session key
  // spells it for a dashboard chat (`dashboard:<key>`), as the fork tag does.
  const [parentKey, setParentKey] = useState(() => parentSession.replace(/^dashboard:/, ''))
  const parentTitle = useAppSelector(s => s.dashboard.slots.find(x => x.key === parentKey)?.title)
  const descriptionId = useId()
  const textId = useId()
  const labelId = useId()
  const hintId = useId()
  const countId = useId()
  const tooLongId = useId()
  const cutId = useId()
  const [phase, setPhase] = useState<Phase>({ kind: 'drafting' })
  const [text, setText] = useState('')
  const [failure, setFailure] = useState('')
  // A merge refused for a draft that can no longer be merged (DRAFT_OUTDATED).
  // Merge stays off until a new draft arrives; a failed redraft leaves it off.
  const [outdated, setOutdated] = useState(false)
  // True while a new draft is being written behind edited text. The text is
  // still at risk even though the ready form is temporarily out of view.
  const [keeping, setKeeping] = useState(false)
  // "Draft again" or a close over text the person edited: the question stands
  // until they answer it, since either would lose every edit. One at a time: the
  // later request replaces the earlier question.
  const [confirming, setConfirming] = useState<'redraft' | 'close' | null>(null)
  const [opening, setOpening] = useState(false)
  // Set on every mount, not only cleared on unmount: StrictMode unmounts and
  // remounts each effect once in development, and a flag that stayed cleared
  // would drop every answer after that.
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  // `onScreen` is the draft the person asked to replace. A failed redraft goes
  // back to it, and to their edits, rather than to a refusal that drops both.
  // `keepText` adopts the new draft without touching the text: the covers line
  // and the fingerprint Merge sends follow the draft, the words stay the person's.
  const draftNow = useCallback(async (onScreen?: MergeBackDraft, { keepText = false } = {}) => {
    setKeeping(keepText)
    setPhase({ kind: 'drafting' })
    setFailure('')
    try {
      const draft = await api.mergeBackDraft(forkKey)
      if (!live.current) return
      setParentKey(draft.parent)
      if (!keepText) setText(initialMergeText(draft))
      setOutdated(false)
      setKeeping(false)
      setPhase({ kind: 'ready', draft })
    } catch (err) {
      if (!live.current) return
      setKeeping(false)
      if (onScreen) {
        setPhase({ kind: 'ready', draft: onScreen })
        setFailure(mergeBackErrorMessage(err, parentSession))
        return
      }
      const code = mergeBackErrorCode(err)
      const state = stateCodeOf(code)
      if (state) {
        setPhase({ kind: 'state', code: state, message: mergeBackErrorMessage(err, parentSession) })
        return
      }
      setPhase({ kind: 'refused', code, message: mergeBackErrorMessage(err, parentSession), report: reportForError(err) })
    }
  }, [forkKey, parentSession])

  // Asked for once per form. A second request from StrictMode's remount would
  // meet the gateway's refusal of a draft while another is being written.
  const asked = useRef(false)
  useEffect(() => {
    if (asked.current) return
    asked.current = true
    void draftNow()
  }, [draftNow])

  const openParent = async () => {
    setOpening(true)
    let failed: { report: ErrorReport | undefined } | null = null
    try {
      // The History tab's own reopen. It also makes the parent the active chat,
      // which is where the person is going once the merge lands.
      const opened = await dispatch(resumeFromHistory({ key: parentSession, title: '' })).unwrap()
      if (!opened.ok) failed = { report: undefined }
    } catch (err) {
      failed = { report: reportForError(err) }
    } finally {
      if (live.current) setOpening(false)
    }
    if (!live.current) return
    if (failed) {
      // Still refused for the same reason, now saying why the remedy failed.
      setPhase({ kind: 'refused', code: 'parent_not_open', message: i18nT('components.mergeBackDialog.parent_not_reopened'), report: failed.report })
      return
    }
    void draftNow()
  }

  // Text the person changed from what the current draft opened with. During a
  // keep-text redraft the form is out of view, but closing would still discard
  // the preserved text.
  const edited = keeping || (phase.kind === 'ready' && text !== initialMergeText(phase.draft))

  // Unedited text is replaced at once: the person loses nothing they wrote.
  const redraft = () => {
    if (phase.kind !== 'ready') return
    if (edited) {
      setConfirming('redraft')
      return
    }
    void draftNow(phase.draft)
  }

  const replaceEdits = () => {
    if (phase.kind !== 'ready') return
    setConfirming(null)
    void draftNow(phase.draft)
  }

  // The keep answer after an outdated refusal: a new draft behind the text the
  // person wrote, which is what lets Merge send that text again.
  const keepEditsUnderNewDraft = () => {
    if (phase.kind !== 'ready') return
    setConfirming(null)
    void draftNow(phase.draft, { keepText: true })
  }

  // Every close the person asks for: the X, Escape, a click outside and Cancel.
  // Edited text is the one thing a close loses, so over it the dialog asks the
  // way "Draft again" does. A landed merge and the hand-off close through
  // `onClose` directly: the text is already in the parent, or was never there.
  const requestClose = () => {
    if (phase.kind === 'merging') return
    if (edited) {
      setConfirming('close')
      return
    }
    onClose()
  }

  const merge = async () => {
    if (phase.kind !== 'ready') return
    setConfirming(null)
    const summary = text.trim()
    if (!summary || charCount(summary) > MAX_MERGE_SUMMARY_CHARS) return
    const { draft } = phase
    setPhase({ kind: 'merging', draft })
    setFailure('')
    try {
      const result = await api.mergeBack(forkKey, summary, draft.through, draft.digest)
      if (!live.current) return
      if (result.deferred) {
        // The card is held until the parent's turn ends, so moving the person
        // there now would show them nothing new.
        setPhase({ kind: 'held', parent: result.parent })
        return
      }
      onClose()
      void dispatch(switchSlot(result.parent))
      focusComposer()
    } catch (err) {
      if (!live.current) return
      const code = mergeBackErrorCode(err)
      setFailure(mergeBackErrorMessage(err, parentSession))
      if (code && DRAFT_OUTDATED.has(code)) {
        setOutdated(true)
        setPhase({ kind: 'ready', draft })
        return
      }
      setPhase({ kind: 'ready', draft })
    }
  }

  const goToParent = (key: string) => {
    onClose()
    void dispatch(switchSlot({ key, announceOnMissing: true }))
  }

  const busy = phase.kind === 'drafting' || phase.kind === 'merging'
  const tooLong = charCount(text.trim()) > MAX_MERGE_SUMMARY_CHARS
  const canMerge = phase.kind === 'ready' && !outdated && !!text.trim() && !tooLong
  // The question "Draft again" asks over an outdated draft states the refusal
  // and both ways forward, so the refusal notice stands aside while it is open.
  const outdatedQuestionOpen = confirming === 'redraft' && outdated
  const title = parentTitle
    ? i18nT('components.mergeBackDialog.title_named', { parent: parentTitle })
    : i18nT('components.mergeBackDialog.title')

  const cut = (phase.kind === 'ready' || phase.kind === 'merging') && draftWasCut(phase.draft)
  const describedBy = [hintId, countId, tooLong && tooLongId, cut && cutId].filter(Boolean).join(' ')
  // What a merge does is worth a sentence while there is a draft to read or
  // merge. A refusal, a state line or the held notice says all there is to say,
  // and the sentence above it would only repeat what the person has read.
  const described = phase.kind === 'drafting' || phase.kind === 'ready' || phase.kind === 'merging'

  return (
    <Dialog open onOpenChange={open => { if (!open) requestClose() }}>
      <DialogContent
        maxWidth={560}
        // Radix warns when this names an id that is not on screen, and accepts
        // `undefined` for a dialog whose status line is its only text.
        aria-describedby={described ? descriptionId : undefined}
        // Not closable while the merge is being written: closing would not stop
        // it, it would only hide whether it landed.
        hideClose={phase.kind === 'merging'}
        onEscapeKeyDown={e => { if (phase.kind === 'merging') e.preventDefault() }}
        onPointerDownOutside={e => { if (phase.kind === 'merging') e.preventDefault() }}
      >
        <form className="flex min-h-0 flex-1 flex-col" onSubmit={e => { e.preventDefault(); void merge() }}>
          <DialogHeader>
            <GitMerge size={15} className="shrink-0 text-muted" aria-hidden="true" />
            <DialogTitle>{title}</DialogTitle>
          </DialogHeader>
          <DialogBody>
            <div className="flex flex-col gap-3">
              {described && (
                <DialogDescription id={descriptionId}>{i18nT('components.mergeBackDialog.description')}</DialogDescription>
              )}
              {phase.kind === 'drafting' && (
                <div role="status" aria-live="polite" className="flex items-center gap-2 text-[13px] text-muted" data-testid="merge-back-drafting">
                  <Loader2 size={13} className="animate-spin" aria-hidden="true" />
                  {i18nT('components.mergeBackDialog.drafting')}
                </div>
              )}
              {phase.kind === 'drafting' && keeping && confirming === 'close' && (
                <EditsQuestion
                  testId="merge-back-close"
                  question={i18nT('components.mergeBackDialog.close_confirm')}
                  onKeep={() => setConfirming(null)}
                  action={{ label: i18nT('components.mergeBackDialog.close_discard'), testId: 'merge-back-close-discard', onClick: onClose }}
                />
              )}
              {phase.kind === 'state' && (
                // Where the chats stand, in the dialog's own muted register: the
                // one action is a footer button or Close, and nothing is wrong to
                // hand to the agent.
                <p role="status" className="flex items-start gap-1.5 text-[13px] leading-5 text-muted" data-testid="merge-back-refused">
                  <Info size={13} className="mt-1 shrink-0" aria-hidden="true" />
                  <span>{phase.message}</span>
                </p>
              )}
              {phase.kind === 'refused' && (
                <ErrorNotice
                  message={phase.message}
                  report={phase.report}
                  askAgent
                  // Otherwise the dialog stays open over the chat the hand-off opens.
                  onHandoff={onClose}
                  testId="merge-back-refused"
                />
              )}
              {(phase.kind === 'ready' || phase.kind === 'merging') && (
                <div className="flex flex-col gap-1.5">
                  {/* The row wraps: on a phone the label takes the width it needs
                      and the count drops under it, flush with the text box's
                      right edge, instead of running past it. */}
                  <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 text-[12px]">
                    <label id={labelId} htmlFor={textId} className="min-w-0 font-medium text-text">
                      {phase.draft.remaining > 0
                        ? i18nT('components.mergeBackDialog.covers_partial', { covered: phase.draft.messages, count: phase.draft.messages + phase.draft.remaining })
                        : i18nT('components.mergeBackDialog.covers', { count: phase.draft.messages })}
                    </label>
                    <span className="ml-auto flex items-baseline gap-2 text-right">
                      <span id={countId} className={`shrink-0 ${tooLong ? 'text-danger' : 'text-muted'}`} data-testid="merge-back-count">
                        {i18nT('components.mergeBackDialog.length', { chars: charCount(text.trim()), max: MAX_MERGE_SUMMARY_CHARS })}
                      </span>
                    </span>
                  </div>
                  {phase.draft.remaining > 0 && (
                    <p className="text-[12px] text-muted" data-testid="merge-back-remaining">
                      {i18nT('components.mergeBackDialog.remaining', { count: phase.draft.remaining })}
                    </p>
                  )}
                  <textarea
                    id={textId}
                    autoFocus
                    value={text}
                    onChange={e => setText(e.target.value)}
                    disabled={phase.kind === 'merging'}
                    rows={10}
                    aria-labelledby={labelId}
                    aria-describedby={describedBy}
                    aria-invalid={tooLong || undefined}
                    placeholder={i18nT('components.mergeBackDialog.placeholder')}
                    className="min-h-[160px] w-full resize-y rounded-md border border-border bg-bg-elevated px-3 py-2 font-body text-sm leading-relaxed text-text outline-hidden transition-colors focus-ring"
                    data-testid="merge-back-text"
                  />
                  {tooLong && (
                    <p id={tooLongId} className="flex items-start gap-1.5 text-[12px] leading-4 text-danger" data-testid="merge-back-too-long">
                      <AlertCircle size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
                      <span>{i18nT('utils.mergeBackError.summary_too_long')}</span>
                    </p>
                  )}
                  {cut && (
                    <p id={cutId} className="flex items-start gap-1.5 text-[12px] leading-4 text-warn" data-testid="merge-back-cut">
                      <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
                      <span>{i18nT('components.mergeBackDialog.cut')}</span>
                    </p>
                  )}
                  <div className="flex items-start justify-between gap-3">
                    <span id={hintId} className="text-[12px] text-muted">{i18nT('components.mergeBackDialog.edit_hint')}</span>
                    <Btn
                      type="button"
                      onClick={redraft}
                      disabled={busy || confirming !== null}
                      className="shrink-0 px-2 py-0.5 text-[12px]"
                      data-testid="merge-back-redraft"
                    >
                      <RotateCw size={12} aria-hidden="true" /> {i18nT('components.mergeBackDialog.draft_again')}
                    </Btn>
                  </div>
                  {outdatedQuestionOpen && (
                    // Here keeping the text is not a cancel: the draft behind it
                    // has to be drafted again before Merge can send the text.
                    <EditsQuestion
                      testId="merge-back-redraft-outdated"
                      question={i18nT('components.mergeBackDialog.redraft_outdated_confirm')}
                      keepLabel={i18nT('components.mergeBackDialog.redraft_outdated_keep')}
                      onKeep={keepEditsUnderNewDraft}
                      action={{ label: i18nT('components.mergeBackDialog.redraft_replace'), testId: 'merge-back-redraft-replace', onClick: replaceEdits }}
                    />
                  )}
                  {confirming === 'redraft' && !outdated && (
                    <EditsQuestion
                      testId="merge-back-redraft"
                      question={i18nT('components.mergeBackDialog.redraft_confirm')}
                      onKeep={() => setConfirming(null)}
                      action={{ label: i18nT('components.mergeBackDialog.redraft_replace'), testId: 'merge-back-redraft-replace', onClick: replaceEdits }}
                    />
                  )}
                  {confirming === 'close' && (
                    <EditsQuestion
                      testId="merge-back-close"
                      question={i18nT('components.mergeBackDialog.close_confirm')}
                      onKeep={() => setConfirming(null)}
                      action={{ label: i18nT('components.mergeBackDialog.close_discard'), testId: 'merge-back-close-discard', onClick: onClose }}
                    />
                  )}
                </div>
              )}
              {phase.kind === 'held' && (
                <p role="status" className="text-[13px] text-text" data-testid="merge-back-held">
                  {i18nT('components.mergeBackDialog.held')}
                </p>
              )}
              {/* No hand-off: this notice shows only under the summary, which the
                  person may have edited and nothing saves until Merge. The hand-off
                  navigates to chat and unmounts this dialog, text and all. */}
              {failure && !outdatedQuestionOpen && <ErrorNotice message={failure} testId="merge-back-error" />}
            </div>
          </DialogBody>
          <DialogFooter>
            {phase.kind === 'held' ? (
              <>
                <Btn type="button" onClick={onClose} data-testid="merge-back-close">{i18nT('components.mergeBackDialog.close')}</Btn>
                <Btn type="button" primary onClick={() => goToParent(phase.parent)} data-testid="merge-back-open-parent">
                  {i18nT('components.mergeBackDialog.open_parent')}
                </Btn>
              </>
            ) : (
              <>
                <Btn type="button" onClick={requestClose} disabled={phase.kind === 'merging'} data-testid="merge-back-cancel">
                  {phase.kind === 'refused' || phase.kind === 'state' ? i18nT('components.mergeBackDialog.close') : i18nT('components.mergeBackDialog.cancel')}
                </Btn>
                {(phase.kind === 'refused' || phase.kind === 'state') && phase.code === 'parent_not_open' && (
                  <Btn type="button" primary onClick={() => void openParent()} disabled={opening} aria-busy={opening} data-testid="merge-back-reopen-parent">
                    {opening && <Loader2 size={13} className="animate-spin" aria-hidden="true" />} {i18nT('components.mergeBackDialog.open_parent')}
                  </Btn>
                )}
                {phase.kind === 'refused' && phase.code !== 'parent_not_open' && !DRAFT_FINAL.has(phase.code ?? '') && (
                  <Btn type="button" onClick={() => void draftNow()} data-testid="merge-back-retry">
                    {i18nT('components.mergeBackDialog.draft_again')}
                  </Btn>
                )}
                {(phase.kind === 'ready' || phase.kind === 'merging' || phase.kind === 'drafting') && (
                  <Btn type="submit" primary disabled={!canMerge} aria-busy={phase.kind === 'merging'} data-testid="merge-back-merge">
                    {phase.kind === 'merging'
                      ? <><Loader2 size={13} className="animate-spin" aria-hidden="true" /> {i18nT('components.mergeBackDialog.merging')}</>
                      : i18nT('components.mergeBackDialog.merge')}
                  </Btn>
                )}
              </>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The question asked before an action loses the person's edited text. Keeping is
 * the default answer and takes the focus: it is the one answer that cannot lose
 * anything. `keepLabel` names what keeping does when it is more than closing the
 * question, as it is after an outdated refusal.
 */
function EditsQuestion({ testId, question, keepLabel, onKeep, action }: {
  testId: string
  question: string
  keepLabel?: string
  onKeep: () => void
  action: { label: string; testId: string; onClick: () => void }
}) {
  const questionId = useId()
  return (
    <div
      role="alertdialog"
      aria-labelledby={questionId}
      className="rounded-lg border border-border bg-bg p-2.5 text-[12px] leading-[18px]"
      data-testid={`${testId}-confirm`}
    >
      <b id={questionId}>{question}</b>
      <div className="mt-2 flex flex-wrap gap-2">
        <Btn type="button" primary autoFocus className="px-2 py-0.5 text-[12px]" onClick={onKeep} data-testid={`${testId}-keep`}>
          {keepLabel ?? i18nT('components.mergeBackDialog.redraft_keep')}
        </Btn>
        <Btn type="button" className="px-2 py-0.5 text-[12px]" onClick={action.onClick} data-testid={action.testId}>
          {action.label}
        </Btn>
      </div>
    </div>
  )
}
