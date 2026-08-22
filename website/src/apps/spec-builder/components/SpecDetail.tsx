// SpecDetail — the selected spec: title row + working-dir breadcrumb, then a
// draggable chat | docs split. The docs card carries the tab header, phase-gated
// approval / build actions, the fullscreen review overlay, and a stacked review
// comments tray fed by selection-to-comment in DocView.
import { useState, useEffect, useRef, useCallback } from 'react'
import { motion } from 'framer-motion'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Maximize2, Minimize2, Play, Pause, MessageSquare, X } from 'lucide-react'
import { ADVANCE_PROMPT } from '../prompts'
import { specApi, LS, phaseLabel, PHASE_BUILDING_KEY, type SpecDetail as SpecDetailData } from '../api'
import { ACCENT, SEL_BG, SEL_BORDER, PULSE_MOTION, Btn } from './shared'
import SegmentedControl, { type Segment } from '../../../components/SegmentedControl'
import { useIsMobile } from '../../../hooks/useIsMobile'
import ChatColumn from './ChatColumn'
import DocView from './DocView'
import { DOC_CSS } from '../inlineStyles'
import {
  REVIEW_FEEDBACK_HEADER,
  reviewFeedbackFileHeader,
  reviewFeedbackItem,
} from '../prompts'
import SpecStatePanel from './SpecStatePanel'
import { ChatColumnSkeleton } from './Shimmer'

import { i18nT } from '../../../i18n/t'
export interface ReviewComment {
  id: string
  file: string
  quote: string
  note: string
}

const DOC_TABS = [
  { id: 'requirements', labelKey: 'apps.specBuilder.components.specDetail.tab_requirements' },
  { id: 'design', labelKey: 'apps.specBuilder.components.specDetail.tab_design' },
  { id: 'tasks', labelKey: 'apps.specBuilder.components.specDetail.tab_tasks' },
] as const

type DocTabId = (typeof DOC_TABS)[number]['id']

/** How long after a dispatched instruction the detail poll stays fast. The slot's
 *  ``running`` flag is what normally selects the fast cadence, and it is still
 *  false when the POST returns, so this window covers the gap. */
const SEND_FOLLOWUP_MS = 20000

// The button label is copy and is translated; ``msg`` is the instruction sent to
// the agent and lives in prompts.ts, deliberately untranslated. ``target`` is the
// document the agent will write next: approving switches to it, so the drafting
// skeleton is what the user sees instead of the document they just approved.
const ADVANCE: Record<string, { labelKey: string; pendingKey: string; target: DocTabId; msg: string }> = {
  requirements: {
    labelKey: 'apps.specBuilder.components.specDetail.advance_to_design',
    pendingKey: 'apps.specBuilder.components.specDetail.drafting_design',
    target: 'design',
    msg: ADVANCE_PROMPT.requirements,
  },
  design: {
    labelKey: 'apps.specBuilder.components.specDetail.advance_to_tasks',
    pendingKey: 'apps.specBuilder.components.specDetail.drafting_tasks',
    target: 'tasks',
    msg: ADVANCE_PROMPT.design,
  },
}

export interface SpecDetailProps {
  name: string
  setErr: (msg: string) => void
}

export default function SpecDetail({ name, setErr }: SpecDetailProps) {
  const [tab, setTab] = useState<DocTabId>('requirements')
  const [expanded, setExpanded] = useState(false)
  // Narrow: the document column steps aside and the chat takes the full width.
  // The document is still reachable — the same fullscreen review overlay, opened
  // from the chat header instead of from the hidden column's own header.
  const isMobile = useIsMobile()

  // React Query rather than useState + setInterval, for the same reason the
  // specs list uses it: two overlapping manual polls could resolve OUT OF ORDER,
  // so a slow 2.5s request landing after a faster later one reverted the docs
  // pane and the phase pill to stale content. React Query keeps one in-flight
  // request per key and discards superseded results. The poll stays fast while
  // the agent works and slows down when it is idle.
  // The identity every mutation carries: what THIS view rendered.
  const specId = () => ({ spec_dir: detail?.spec_dir, slot_key: detail?.slot_key })

  // When this view last dispatched an instruction. ``running`` is derived from the
  // worker slot, and the slot is not running yet when the POST returns — the turn
  // is dispatched, not awaited. On the idle 6s cadence the whole UI therefore sat
  // unchanged for seconds after a click, so the approval looked like it had not
  // registered. A ref, not state: refetchInterval is read per fetch and this must
  // not itself trigger a render.
  const lastSendAt = useRef(0)

  const detailQuery = useQuery({
    queryKey: ['spec-builder', 'spec', name],
    queryFn: () => specApi.get(name),
    refetchInterval: (q) => {
      const d = q.state.data
      if (d?.running || d?.status === 'executing') return 2500
      // Catch the slot coming up after a dispatch, then fall back to idle.
      if (Date.now() - lastSendAt.current < SEND_FOLLOWUP_MS) return 1200
      return 6000
    },
  })
  const detail: SpecDetailData | null = detailQuery.data ?? null

  // Surface a fetch error without clobbering the last good document content.
  useEffect(() => {
    if (detailQuery.error) setErr((detailQuery.error as Error).message)
  }, [detailQuery.error, setErr])

  const running = !!detail?.running
  const executing = detail?.status === 'executing'

  // Draggable split: % of the body width given to the docs column (persisted).
  const bodyRef = useRef<HTMLDivElement>(null)
  const [docPct, setDocPctRaw] = useState(() => {
    try { const v = Number(localStorage.getItem(LS.docPct)); return v >= 25 && v <= 75 ? v : 44 } catch { return 44 }
  })
  const setDocPct = (v: number) => { setDocPctRaw(v); try { localStorage.setItem(LS.docPct, String(v)) } catch { /* ignore */ } }
  const onDividerDown = (e: React.MouseEvent) => {
    e.preventDefault()
    const onMove = (ev: MouseEvent) => {
      if (!bodyRef.current) return
      const r = bodyRef.current.getBoundingClientRect()
      setDocPct(Math.min(75, Math.max(25, ((r.right - ev.clientX) / r.width) * 100)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.body.style.cursor = 'col-resize'
    // Lock text selection for the duration of the drag — without this, moving
    // the pointer over the document selects prose (and can raise the
    // selection-to-comment pill). Issue Radar's splitter does the same.
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }
  // Keyboard resize — the divider must be operable without a pointer.
  const onDividerKey = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    setDocPct(Math.min(75, Math.max(25, docPct + (e.key === 'ArrowLeft' ? 4 : -4))))
  }

  // Esc closes the fullscreen review overlay.
  useEffect(() => {
    if (!expanded) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded])

  const hasTasks = !!detail?.files?.['tasks.md']

  // Every action that changes server state goes through a mutation that
  // invalidates BOTH query keys. Refetching only the detail left the rail's
  // status pill stale until its own 15s poll caught up, so "Start building"
  // looked like it had not taken effect in the list.
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void detailQuery.refetch()
    void queryClient.invalidateQueries({ queryKey: ['spec-builder', 'specs'] })
  }, [detailQuery, queryClient])

  const executeMutation = useMutation({
    mutationFn: () => specApi.execute(name, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  const stopMutation = useMutation({
    mutationFn: () => specApi.stop(name, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  // ONE mutation for every message this view sends (phase approval, review
  // feedback, decision answers). Direct specApi.message calls refetched only the
  // detail, so updated_at changed without the specs list knowing and the rail's
  // ordering went stale until its own 15s poll.
  const messageMutation = useMutation({
    mutationFn: (msg: string) => specApi.message(name, msg, specId()),
    onMutate: () => { lastSendAt.current = Date.now() },
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  // Decision answers go out on their own mutation because they carry the decision
  // id: the backend records that id and refuses a second answer for it, so a
  // settled decision cannot be changed later from a re-rendered card.
  const decisionMutation = useMutation({
    mutationFn: (v: { id: string; option: string; msg: string }) =>
      specApi.answerDecision(name, v.id, v.option, v.msg, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })

  // Whether the instruction currently in flight is THIS view's phase approval.
  // messageMutation is shared with the decision tray and the review-comment
  // tray, so keying the button's "Sending…" label on its isPending flag made the
  // approval control claim it was sending while a DECISION answer was in flight.
  const [advancing, setAdvancing] = useState(false)

  // The phase this view approved, held until the backend reports a different one.
  // ``phase`` is DERIVED from which documents exist on disk, so it stays on the
  // approved phase until the agent has written the next file — up to a minute. The
  // button used to spring back to "Approve → Design" in that window, which read as
  // "nothing happened" and invited a second approval into the same turn.
  const [approved, setApproved] = useState<string | null>(null)
  useEffect(() => {
    if (approved && detail?.phase && detail.phase !== approved) setApproved(null)
  }, [approved, detail?.phase])

  // ``mutateAsync`` in a try/finally, NOT mutate()'s per-call callbacks: those
  // live on the mutation observer, and this one mutation is shared with the
  // decision tray and the review-comment tray. A decision answered while a slow
  // approval was still in flight REPLACED the approval's callbacks, so
  // setAdvancing(false) never ran — the control kept the "Sending…" label while
  // isPending went false underneath it, leaving it enabled and able to queue a
  // second approval turn. The promise is per call, so it cannot be displaced.
  const advance = async () => {
    const phase = detail?.phase
    const a = phase ? ADVANCE[phase] : undefined
    if (!a || !phase) return
    setAdvancing(true)
    try {
      await messageMutation.mutateAsync(a.msg)
      setApproved(phase)
      // Switch to the document being written: DocView holds its shape with a
      // drafting skeleton, so there is something to watch instead of the file
      // that was just approved.
      setTab(a.target)
    } catch {
      // Surfaced by the mutation's onError. Nothing is held: the phase was not
      // approved, so the button must offer the approval again.
    } finally {
      setAdvancing(false)
    }
  }
  const execute = () => executeMutation.mutate()
  const stop = () => stopMutation.mutate()

  // ── stacked review comments (highlight → comment → stack → send all) ──
  const [comments, setComments] = useState<ReviewComment[]>([])
  const [sendingAll, setSendingAll] = useState(false)
  const addComment = useCallback((c: Omit<ReviewComment, 'id'>) => {
    setComments((cs) => [...cs, { ...c, id: Date.now() + ':' + cs.length }])
  }, [])
  const removeComment = (id: string) => setComments((cs) => cs.filter((c) => c.id !== id))
  const sendAll = async () => {
    if (!comments.length) return
    setSendingAll(true)
    const byFile: Record<string, ReviewComment[]> = {}
    for (const c of comments) (byFile[c.file] = byFile[c.file] || []).push(c)
    let msg = REVIEW_FEEDBACK_HEADER
    for (const [file, items] of Object.entries(byFile)) {
      msg += reviewFeedbackFileHeader(file)
      items.forEach((c, idx) => {
        msg += reviewFeedbackItem(idx + 1, c.quote, c.note) + '\n\n'
      })
    }
    try {
      await messageMutation.mutateAsync(msg)
      setComments([])
    } catch { /* surfaced by the mutation's onError */ } finally { setSendingAll(false) }
  }

  // Status dot per document, carried as the segment's icon so the shared
  // SegmentedControl can own tab layout (and its responsive collapse).
  const docSegments: Segment<DocTabId>[] = DOC_TABS.map((t, ti) => {
    const fname = t.id + '.md'
    const exists = !!detail?.files?.[fname]
    const firstMissing = DOC_TABS.findIndex((d) => !detail?.files?.[d.id + '.md'])
    const status = exists ? 'ready' : ti === firstMissing ? 'pending' : 'blocked'
    const dotColor = exists ? 'var(--ok)' : status === 'pending' ? 'var(--warn)' : 'var(--muted)'
    const dot = status === 'pending' && running
      ? <motion.span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: dotColor }} {...PULSE_MOTION} />
      : <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: dotColor, opacity: status === 'blocked' ? 0.4 : 1 }} />
    return { key: t.id, label: i18nT(t.labelKey), icon: dot, tooltip: fname + ' — ' + status }
  })

  // Doc column header: shared segmented tabs + expand + phase-gated actions.
  // On a desktop this matches the chat column header's height and bottom border
  // so the two line up. While narrow the columns are STACKED, so that alignment
  // buys nothing and the fixed height costs the phase control: at 390px the row
  // measures 414px against a 390px viewport, and the `overflow-hidden` on the
  // pane clips the action with no way to scroll to it. Wrapping puts the action
  // on its own line instead, fully reachable.
  const docTabsHeader = (fullscreen: boolean) => (
    <div className={`flex gap-1.5 items-center px-2.5 border-b border-border shrink-0 ${
      isMobile && !fullscreen ? 'flex-wrap min-h-[52px] py-1.5' : 'h-[52px]'}`}>
      <SegmentedControl<DocTabId>
        segments={docSegments}
        value={tab}
        onChange={setTab}
        layoutId={fullscreen ? 'sb-doc-tabs-full' : 'sb-doc-tabs'}
      />
      <span className="flex-1" />
      <Btn
        onClick={() => setExpanded(!fullscreen)}
        title={fullscreen ? i18nT('apps.specBuilder.components.specDetail.close_esc') : i18nT('apps.specBuilder.components.specDetail.expand_for_review_esc_to_close')}
        ariaLabel={fullscreen ? i18nT('apps.specBuilder.components.specDetail.close_review_view') : i18nT('apps.specBuilder.components.specDetail.expand_document_for_review')}
        label={fullscreen ? <Minimize2 className="lucide-inline" /> : <Maximize2 className="lucide-inline" />}
      />
      {!fullscreen && !executing && detail?.phase && ADVANCE[detail.phase] && (
        (() => {
          const a = ADVANCE[detail.phase]
          const waiting = approved === detail.phase
          return (
            <Btn
              label={advancing
                ? i18nT('apps.specBuilder.components.specDetail.sending')
                : waiting
                  ? i18nT(a.pendingKey)
                  : <><Play className="lucide-inline" /> {i18nT(a.labelKey)}</>}
              primary={!waiting}
              disabled={advancing || messageMutation.isPending || waiting}
              title={waiting
                ? i18nT('apps.specBuilder.components.specDetail.the_agent_is_writing_the_next_document')
                : i18nT('apps.specBuilder.components.specDetail.tells_the_agent_this_phase_is_approved_and_to_mo')}
              onClick={() => { void advance() }}
            />
          )
        })()
      )}
      {!fullscreen && (executing
        ? (
          <Btn
            label={<><Pause className="lucide-inline" /> {stopMutation.isPending ? i18nT('apps.specBuilder.components.specDetail.pausing') : i18nT('apps.specBuilder.components.specDetail.pause')}</>}
            danger
            disabled={stopMutation.isPending}
            onClick={stop}
          />
        )
        : hasTasks && (
          // Disabled while the handoff is in flight. Two clicks queued TWO
          // handoffs, and Pause halts the running turn while leaving the queued
          // one intact -- so execution resumed by itself and kept editing files
          // after the user had stopped it.
          <Btn
            label={<><Play className="lucide-inline" /> {executeMutation.isPending ? i18nT('apps.specBuilder.components.specDetail.starting') : i18nT('apps.specBuilder.components.specDetail.start_building')}</>}
            primary
            disabled={executeMutation.isPending}
            title={i18nT('apps.specBuilder.components.specDetail.an_agent_will_work_through_the_task_list')}
            onClick={execute}
          />
        )
      )}
    </div>
  )

  return (
    <div ref={bodyRef} className={`flex flex-1 min-w-0 min-h-0 ${isMobile ? 'flex-col' : ''}`}>
      <style>{DOC_CSS}</style>

      {/* ── Chat column ──
          Its own header carries the spec identity (name + phase + working dir),
          so the column is anchored instead of floating under a page-wide title
          band. Issue Radar's detail column does the same: every column owns its
          header, and the headers line up. */}
      <section className="flex-1 min-w-0 flex flex-col">
        <header className="shrink-0 h-[52px] px-4 border-b border-border flex items-center gap-2.5">
          <span className="text-[15px] font-bold tracking-tight text-text-strong overflow-hidden text-ellipsis whitespace-nowrap">{name}</span>
          <span
            className="text-[12px] font-mono px-2.5 py-[3px] rounded-full whitespace-nowrap shrink-0"
            style={{ color: ACCENT, background: SEL_BG }}
          >
            {executing ? i18nT(PHASE_BUILDING_KEY) : phaseLabel(detail?.phase || '') || '…'}
          </span>
          {/* Announce agent activity to assistive tech as it changes. */}
          <span aria-live="polite" className="inline-flex items-center shrink-0">
            {running && (
              <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold whitespace-nowrap" style={{ color: ACCENT }}>
                <motion.span className="w-[7px] h-[7px] rounded-full" style={{ background: ACCENT }} {...PULSE_MOTION} />
                {i18nT('apps.specBuilder.components.specDetail.working')}
              </span>
            )}
          </span>
          <span className="flex-1 min-w-0" />
          {/* Narrow only: the document column is not on screen, and the control
              that opens it lives in that column's own header. This is the same
              fullscreen review overlay, reached from the header that IS visible
              — so the document stays reachable without a second mechanism. */}
          {isMobile && (
            <Btn
              onClick={() => setExpanded(true)}
              title={i18nT('apps.specBuilder.components.specDetail.expand_for_review_esc_to_close')}
              ariaLabel={i18nT('apps.specBuilder.components.specDetail.expand_document_for_review')}
              label={<Maximize2 className="lucide-inline" />}
            />
          )}
          <span
            className="text-[11px] font-mono text-muted overflow-hidden text-ellipsis whitespace-nowrap max-w-[45%]"
            style={{ direction: 'rtl', textAlign: 'right' }}
            title={detail?.working_dir || ''}
          >
            {detail?.working_dir || ''}
          </span>
        </header>
        <div className="flex-1 min-h-0 flex flex-col">
          {/* Gated on the detail load. ChatColumn's embedded chat talks to
              /api/chat, and for a spec DISCOVERED on disk the worker slot does
              not exist yet -- whoever creates it first decides whether it is
              scoped. The app's own detail endpoint creates it with _app and
              project set (_ensure_worker_slot); /api/chat would create it bare,
              so an approved tool would run in the gateway's directory instead of
              the project. Waiting for detail guarantees our endpoint got there
              first. */}
          {detail
            ? (
              <ChatColumn
                name={name}
                slotKey={detail.slot_key}
                onSend={(msg) => messageMutation.mutateAsync(msg)}
              />
            )
            : <ChatColumnSkeleton />}
        </div>
      </section>
      {/* Resizable splitter. eslint's non-interactive heuristics don't model
          the W3C APG "window splitter" pattern, which is precisely
          role="separator" + tabIndex + aria-valuenow/min/max and IS
          interactive once focusable — arrow keys resize it. Suppressed
          rather than reshaped, because a <button> here would announce the
          wrong role and lose the value semantics. */}
      {/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
        <div
          onMouseDown={onDividerDown}
          onKeyDown={onDividerKey}
          role="separator"
          aria-orientation="vertical"
          aria-label={i18nT('apps.specBuilder.components.specDetail.resize_document_panel')}
          aria-valuenow={Math.round(docPct)}
          aria-valuemin={25}
          aria-valuemax={75}
          tabIndex={0}
          title={i18nT('apps.specBuilder.components.specDetail.drag_or_use_to_resize')}
          className={`w-1.5 shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors focus-ring ${isMobile ? 'hidden' : ''}`}
        />
        {/* eslint-enable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
        {/* ── Docs column ──
            A flush panel with a left border, not a floating card: the card
            treatment made two peer columns look like different kinds of surface
            and left the doc header sitting below the chat's.

            While narrow this column becomes a full-width row UNDER the chat,
            carrying the header (which owns the phase controls), the state panel,
            and the pending-comment tray. Only the document body moves to the
            fullscreen overlay. The tray cannot move there: it holds comments the
            user wrote and has not sent, and `key={sel}` unmounts this component
            on the next spec, so hiding the column outright would make them
            unreachable and then silently discard them.

            The height cap is in `vh`, not a percentage: no ancestor in this
            chain has a definite height, so a percentage max-height does not
            resolve and the bound would be inert. `min-h-0` rather than a pinned
            height: the cap binds before shrinking is ever needed on any real
            geometry, but `vh` is relative to the VIEWPORT while this row lives
            in the viewport MINUS the app header, so a shell shorter than the cap
            would otherwise push the tray past the clip. Without a bound this column is
            `shrink-0` while the chat above is `min-h-0`, so an accumulating
            state panel plus a staged comment could grow past the page shell and
            take the tray out of reach. */}
        <section
          className={`min-w-0 flex flex-col ${isMobile
            ? 'w-full min-h-0 border-t border-border max-h-[60vh] overflow-y-auto'
            : 'border-l border-border'}`}
          style={isMobile ? undefined : { flexBasis: docPct + '%', flexGrow: 0, flexShrink: 0 }}
        >
          {/* Only the document BODY steps aside while narrow. The header stays,
              because it is the sole host of the phase controls -- Approve → Design,
              Approve → Tasks, Start building, Pause. The fullscreen overlay builds
              its own header and never calls `docTabsHeader`, and those actions are
              additionally gated on `!fullscreen`, so hiding this header took the
              only route to them: at phone widths a spec could not be advanced,
              built or paused at all. */}
          <div className={`sb-doc flex flex-col overflow-hidden ${isMobile ? 'shrink-0' : 'flex-1 min-h-0'}`}>
            {docTabsHeader(false)}
            {/* Body HIDDEN, not unmounted: the document itself moves to the
                overlay, but DocView holds an in-progress comment draft. */}
            <div className={`flex-1 min-h-0 flex flex-col ${isMobile ? 'hidden' : ''}`}>
              <DocView detail={detail} tab={tab} addComment={addComment} running={running} />
            </div>
          </div>
          {/* Visible at every width. This is the only surface that shows a
              BLOCKING decision and the only one that can answer it, and the
              overlay does not render it -- hidden, a blocked spec was
              indistinguishable from an idle one. */}
          <SpecStatePanel
            detail={detail}
            answerDecision={(id, option, msg) => decisionMutation.mutateAsync({ id, option, msg })}
          />
          {comments.length > 0 && (
            <div
              className="mt-2.5 rounded-lg bg-bg shrink-0 max-h-[220px] flex flex-col"
              style={{ border: '1px solid ' + SEL_BORDER }}
            >
              <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
                <span className="text-[13px] font-semibold text-text flex-1">
                  {i18nT('apps.specBuilder.components.specDetail.pending_comment', { count: comments.length })}
                </span>
                <Btn label={i18nT('apps.specBuilder.components.specDetail.clear')} onClick={() => setComments([])} />
                <Btn
                  label={sendingAll ? i18nT('apps.specBuilder.components.specDetail.sending') : <><MessageSquare className="lucide-inline" /> {i18nT('apps.specBuilder.components.specDetail.send_all_to_agent')}</>}
                  primary
                  disabled={sendingAll}
                  onClick={sendAll}
                />
              </div>
              <div className="overflow-y-auto px-2.5 py-1.5">
                {comments.map((c) => (
                  <div key={c.id} className="flex gap-2 items-start px-1.5 py-[7px] border-b border-border">
                    <span
                      className="text-[11px] font-bold px-2 py-0.5 rounded-full whitespace-nowrap shrink-0"
                      style={{ color: ACCENT, background: SEL_BG }}
                    >
                      {c.file}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-[11px] text-muted overflow-hidden text-ellipsis whitespace-nowrap">“{c.quote}”</div>
                      <div className="text-[12px] text-text mt-0.5">{c.note}</div>
                    </div>
                    <Btn label={<X className="lucide-inline" />} ariaLabel={i18nT('apps.specBuilder.components.specDetail.remove_comment_on', { document: c.file })} onClick={() => removeComment(c.id)} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

      {/* Fullscreen review overlay — position:absolute within the page container
          so the dashboard sidebar/header stay visible. */}
      {expanded && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={i18nT('apps.specBuilder.components.specDetail.review_document', { document: tab }) + '.md for ' + name}
          tabIndex={-1}
          ref={(el) => el?.focus()}
          className="absolute inset-0 z-[60] bg-bg flex flex-col outline-none"
          style={{ padding: '14px 26px 20px' }}
        >
          <style>{DOC_CSS}</style>
          <div className="flex items-center gap-2.5 mb-2.5 shrink-0">
            <span className="text-[15px] font-bold text-text-strong">{name}</span>
            <span className="text-[12px] font-mono px-2.5 py-[3px] rounded-full" style={{ color: ACCENT, background: SEL_BG }}>{i18nT('apps.specBuilder.components.specDetail.document_file_name', { name: tab })}</span>
            <span className="flex-1" />
            <SegmentedControl<DocTabId>
              segments={DOC_TABS.map((t) => ({ key: t.id, label: i18nT(t.labelKey) }))}
              value={tab}
              onChange={setTab}
              layoutId="sb-doc-tabs-overlay"
            />
            <Btn
              onClick={() => setExpanded(false)}
              title={i18nT('apps.specBuilder.components.specDetail.close_esc')}
              ariaLabel={i18nT('apps.specBuilder.components.specDetail.close_review_view')}
              label={<Minimize2 className="lucide-inline" />}
            />
          </div>
          <div className="flex-1 min-h-0 flex justify-center">
            <div className="sb-doc flex flex-col border border-border rounded-lg bg-card overflow-hidden min-h-0" style={{ width: 'min(980px, 100%)' }}>
              <DocView detail={detail} tab={tab} addComment={addComment} running={running} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
