// SpecDetail — the selected spec: title row + working-dir breadcrumb, then a
// draggable chat | docs split. The docs card carries the tab header, phase-gated
// approval / build actions, the fullscreen review overlay, and a stacked review
// comments tray fed by selection-to-comment in DocView.
import { useState, useEffect, useRef, useCallback } from 'react'
import { motion } from 'framer-motion'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Maximize2, Minimize2, Play, Pause, MessageSquare, X,
  Check, AlertTriangle, Pencil, Archive, ArchiveRestore, Copy, MoreHorizontal, Trash2,
} from 'lucide-react'
import { ADVANCE_PROMPT } from '../prompts'
import {
  specApi, LS, phaseLabel, PHASE_BUILDING_KEY, specDetailPollMs,
  type SpecDetail as SpecDetailData, type SpecIdentity, type SpecTask,
} from '../api'
import { Input } from '../../../components/ui'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../../../components/ui/dropdown-menu'
import { ACCENT, SEL_BG, SEL_BORDER, PULSE_MOTION, Btn } from './shared'
import SegmentedControl, { type Segment } from '../../../components/SegmentedControl'
import { useIsMobile } from '../../../hooks/useIsMobile'
import ChatColumn from './ChatColumn'
import DocView, { type Selection as DocSelection } from './DocView'
import { DOC_CSS } from '../inlineStyles'
import {
  REVIEW_FEEDBACK_HEADER,
  reviewFeedbackFileHeader,
  reviewFeedbackItem,
} from '../prompts'
import SpecStatePanel from './SpecStatePanel'
import { ChatColumnSkeleton } from './Shimmer'
import Modal from '../../../components/Modal'

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
  /** Called after a successful delete so the workspace can drop the selection. */
  onDeleted?: () => void
  onDuplicated?: (name: string) => void
}

export default function SpecDetail({ name, setErr, onDeleted, onDuplicated }: SpecDetailProps) {
  const [tab, setTab] = useState<DocTabId>('requirements')
  const [expanded, setExpanded] = useState(false)
  // Snapshot of the spec that the confirm dialog is about — not the live
  // poll. Another dashboard can delete-and-recreate this name while the
  // dialog is open; reading specId() at click time would remove the replacement.
  const [pendingRemove, setPendingRemove] = useState<SpecIdentity | null>(null)
  const confirmDelete = pendingRemove !== null
  // Comment composer lives here so the column ↔ overlay remount of DocView
  // does not drop a half-typed note. SpecDetail itself remounts on spec change
  // (`key={sel}` in Workspace), which clears it.
  const [docSel, setDocSel] = useState<DocSelection | null>(null)
  const [docNote, setDocNote] = useState<DocSelection | null>(null)
  const [docDraft, setDocDraft] = useState('')
  const docComposer = {
    sel: docSel, setSel: setDocSel, note: docNote, setNote: setDocNote, draft: docDraft, setDraft: setDocDraft,
  }
  // Narrow: the document column steps aside and the chat takes the full width.
  // The document is still reachable — the same fullscreen review overlay, opened
  // from the chat header instead of from the hidden column's own header.
  const isMobile = useIsMobile()

  // React Query rather than useState + setInterval, for the same reason the
  // specs list uses it: two overlapping manual polls could resolve OUT OF ORDER,
  // so a slow 2.5s request landing after a faster later one reverted the docs
  // pane and the phase pill to stale content. React Query keeps one in-flight
  // request per key and discards superseded results. The poll stays fast while
  // the agent works, while the Tasks tab is open, and for a follow-up window
  // after a dispatch or a tasks.md hash change; otherwise it idles.
  // The identity every mutation carries: what THIS view rendered.
  const specId = () => ({ spec_dir: detail?.spec_dir, slot_key: detail?.slot_key })

  // When this view last dispatched an instruction. ``running`` is derived from the
  // worker slot, and the slot is not running yet when the POST returns — the turn
  // is dispatched, not awaited. On the idle cadence the whole UI therefore sat
  // unchanged for seconds after a click, so the approval looked like it had not
  // registered. A ref, not state: refetchInterval is read per fetch and this must
  // not itself trigger a render.
  const lastSendAt = useRef(0)
  // tasks.md is written by the agent and by hand, neither of which hits lastSendAt.
  // Remember the last hash we rendered so a disk change re-arms the fast window
  // even when the user is not on the Tasks tab (they come back to a fresh list).
  const lastTasksHash = useRef<string | undefined>(undefined)
  const lastTasksChangeAt = useRef(0)

  const detailQuery = useQuery({
    queryKey: ['spec-builder', 'spec', name],
    queryFn: ({ signal }) => specApi.get(name, signal),
    // An agent editing tasks.md from the main chat (or an editor) does not
    // focus this window. The default pauses the interval in the background, so
    // the progress view froze until a focus event that Electron does not always
    // deliver.
    refetchIntervalInBackground: true,
    refetchInterval: (q) => {
      const d = q.state.data
      const hash = d?.docs?.['tasks.md']?.hash
      if (hash && lastTasksHash.current && hash !== lastTasksHash.current) {
        lastTasksChangeAt.current = Date.now()
      }
      if (hash) lastTasksHash.current = hash
      return specDetailPollMs({
        running: d?.running,
        status: d?.status,
        watchingTasks: tab === 'tasks',
        msSinceDispatch: Date.now() - lastSendAt.current,
        msSinceTasksChange: Date.now() - lastTasksChangeAt.current,
      })
    },
  })
  const detail: SpecDetailData | null = detailQuery.data ?? null

  // Opening the progress view is itself a reason to read disk now, not at the
  // next idle tick. The interval below keeps it fresh; this is the first look.
  const prevTab = useRef(tab)
  useEffect(() => {
    if (tab === 'tasks' && prevTab.current !== 'tasks') void detailQuery.refetch()
    prevTab.current = tab
  }, [tab, detailQuery])

  // Surface a fetch error without clobbering the last good document content.
  useEffect(() => {
    if (detailQuery.error) setErr((detailQuery.error as Error).message)
  }, [detailQuery.error, setErr])

  const running = !!detail?.running
  const executing = detail?.status === 'executing'
  const hasIdentity = !!(detail?.spec_dir && detail?.slot_key)

  // Draggable split: % of the body width given to the docs column (persisted).
  const bodyRef = useRef<HTMLDivElement>(null)
  const [docPct, setDocPctRaw] = useState(() => {
    try { const v = Number(localStorage.getItem(LS.docPct)); return v >= 25 && v <= 75 ? v : 44 } catch { return 44 }
  })
  const setDocPct = (v: number) => { setDocPctRaw(v); try { localStorage.setItem(LS.docPct, String(v)) } catch { /* ignore */ } }
  // Held so an unmount mid-drag can drop the window listeners and restore the
  // cursor; without this the next pointer move would keep resizing a gone pane.
  const stopDividerDrag = useRef<(() => void) | null>(null)
  const onDividerDown = (e: React.MouseEvent) => {
    e.preventDefault()
    stopDividerDrag.current?.()
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
      stopDividerDrag.current = null
    }
    stopDividerDrag.current = onUp
    document.body.style.cursor = 'col-resize'
    // Lock text selection for the duration of the drag — without this, moving
    // the pointer over the document selects prose (and can raise the
    // selection-to-comment pill). Issue Radar's splitter does the same.
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }
  useEffect(() => () => { stopDividerDrag.current?.() }, [])
  // Keyboard resize — the divider must be operable without a pointer.
  const onDividerKey = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    setDocPct(Math.min(75, Math.max(25, docPct + (e.key === 'ArrowLeft' ? 4 : -4))))
  }

  // Esc closes the fullscreen review overlay. Suppressed while the delete
  // confirm is up so the stacked dialog gets the key first (Modal already
  // skips an Escape that was preventDefault'd).
  useEffect(() => {
    if (!expanded || confirmDelete) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      setExpanded(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded, confirmDelete])

  // Focus the overlay once when it opens. An inline callback ref would re-run
  // on every poll (the function identity changes, so React calls it with the
  // element again) and yank focus out of the comment composer.
  const overlayRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (expanded) overlayRef.current?.focus()
  }, [expanded])

  const hasTasks = !!detail?.files?.['tasks.md']

  // Every action that changes server state goes through a mutation that
  // invalidates BOTH query keys. Refetching only the detail left the rail's
  // status pill stale until its own 15s poll caught up, so "Start building"
  // looked like it had not taken effect in the list.
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void detailQuery.refetch()
    return queryClient.invalidateQueries({ queryKey: ['spec-builder', 'specs'] })
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
  // Delete must NOT refetch this spec: the entry is gone, and a 404 here would
  // paint an error banner over a successful removal. Drop the detail cache and
  // refresh the list; the workspace clears the selection via onDeleted.
  const deleteMutation = useMutation({
    mutationFn: (id: SpecIdentity) => specApi.remove(name, id),
    onError: (e) => setErr((e as Error).message),
    onSuccess: () => {
      setPendingRemove(null)
      void queryClient.removeQueries({ queryKey: ['spec-builder', 'spec', name] })
      void queryClient.invalidateQueries({ queryKey: ['spec-builder', 'specs'] })
      onDeleted?.()
    },
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

  // A selection pill's x/y were measured in the column pane. Expanding remounts
  // DocView in the overlay, so those coordinates would float over empty space.
  useEffect(() => { setDocSel(null) }, [expanded])

  // Decision answers go out on their own mutation because they carry the decision
  // id: the backend records that id and refuses a second answer for it, so a
  // settled decision cannot be changed later from a re-rendered card.
  const decisionMutation = useMutation({
    mutationFn: (v: { id: string; option: string; msg: string }) =>
      specApi.answerDecision(name, v.id, v.option, v.msg, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })

  // ── direct authority over recorded approvals and lifecycle ──
  const approveMutation = useMutation({
    mutationFn: (v: { phase: string; hash: string }) =>
      specApi.approve(name, v.phase, v.hash, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  const runTaskMutation = useMutation({
    mutationFn: (task: SpecTask) => specApi.runTask(name, task.index, task.hash, specId()),
    onMutate: () => { lastSendAt.current = Date.now() },
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  const titleMutation = useMutation({
    mutationFn: (title: string) => specApi.setTitle(name, title, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  const archiveMutation = useMutation({
    mutationFn: (archived: boolean) => specApi.setArchived(name, archived, specId()),
    onError: (e) => setErr((e as Error).message),
    onSettled: invalidate,
  })
  const duplicateMutation = useMutation({
    mutationFn: (newName: string) => specApi.duplicate(name, newName, specId()),
    onSuccess: async (copy) => {
      await invalidate()
      onDuplicated?.(copy.name)
    },
    onError: (e) => setErr((e as Error).message),
  })

  // The task whose run is being dispatched, so the list can mark just that row.
  const [pendingTask, setPendingTask] = useState<number | null>(null)
  const runTask = (task: SpecTask) => {
    setPendingTask(task.index)
    runTaskMutation.mutate(task, { onSettled: () => setPendingTask(null) })
  }

  // Inline label editing. `null` = not editing; the NAME is never touched, only
  // this label (see the backend's title handler for why the name is immutable).
  const [titleDraft, setTitleDraft] = useState<string | null>(null)
  // Duplicate asks for the copy's name inline. It has to be asked for rather than
  // derived, because the name IS the new spec's directory and branch — the one
  // field a copy cannot inherit.
  const [dupDraft, setDupDraft] = useState<string | null>(null)
  const commitTitle = () => {
    const next = (titleDraft ?? '').trim()
    setTitleDraft(null)
    if (next !== (detail?.title ?? '')) titleMutation.mutate(next)
  }
  const commitDuplicate = () => {
    const next = (dupDraft ?? '').trim()
    if (!next) return
    duplicateMutation.mutate(next)
    setDupDraft(null)
  }

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
    // Approval evidence is meaningful only for the document the user can see.
    // A reload can restore a later phase while this local tab starts on
    // requirements, so both the control and this handler must reject that gap.
    if (!phase || tab !== phase) return
    const a = phase ? ADVANCE[phase] : undefined
    const hash = phase ? detail?.docs?.[phase + '.md']?.hash : undefined
    if (!a || !hash) return
    setAdvancing(true)
    try {
      // RECORD the approval before instructing the agent, and let a failure stop
      // the instruction. Approval used to be nothing but this chat message, so the
      // server never knew a phase had been signed off, by whom, or against which
      // text — the only trace was a line in the transcript. Ordering matters: if
      // the record fails the agent must not move on, otherwise the spec advances
      // with no evidence anyone approved it.
      await approveMutation.mutateAsync({ phase, hash })
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

  // Shared by the docs-column header and the fullscreen review overlay. The
  // overlay used to omit these, so expanding to read a document hid the only
  // way to approve, build, or pause it.
  const phaseActions = () => (
    <>
      {!executing && detail?.phase && ADVANCE[detail.phase] && (
        (() => {
          const a = ADVANCE[detail.phase]
          const waiting = approved === detail.phase
          const hasReviewHash = !!detail.docs?.[detail.phase + '.md']?.hash
          const reviewingPhase = tab === detail.phase
          return (
            <Btn
              label={advancing
                ? i18nT('apps.specBuilder.components.specDetail.sending')
                : waiting
                  ? i18nT(a.pendingKey)
                  : <><Play className="lucide-inline" /> {i18nT(a.labelKey)}</>}
              primary={!waiting}
              disabled={
                advancing
                || messageMutation.isPending
                || waiting
                || !hasReviewHash
                || !reviewingPhase
              }
              title={!hasReviewHash
                ? i18nT('apps.specBuilder.components.docView.nothing_here_yet')
                : waiting
                  ? i18nT('apps.specBuilder.components.specDetail.the_agent_is_writing_the_next_document')
                  : i18nT('apps.specBuilder.components.specDetail.tells_the_agent_this_phase_is_approved_and_to_mo')}
              onClick={() => { void advance() }}
            />
          )
        })()
      )}
      {executing
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
        )}
    </>
  )

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
      {/* Recorded approval for the document on screen. `stale` is derived by the
          backend from the hash, so a document the agent rewrote after sign-off
          says so instead of continuing to look approved — which is the whole
          reason the approved VERSION is recorded rather than a bare flag. */}
      {(() => {
        const ap = detail?.approvals?.[tab]
        if (!ap) return null
        return ap.stale
          ? (
            <span
              className="flex items-center gap-1 text-[11px] font-semibold whitespace-nowrap shrink-0"
              style={{ color: 'var(--warn)' }}
              title={i18nT('apps.specBuilder.components.specDetail.this_document_changed_after_it_was_approved')}
            >
              <AlertTriangle size={12} strokeWidth={2} />
              {i18nT('apps.specBuilder.components.specDetail.changed_since_approval')}
            </span>
          )
          : (
            <span
              className="flex items-center gap-1 text-[11px] font-semibold whitespace-nowrap shrink-0"
              style={{ color: 'var(--ok)' }}
              title={ap.user
                ? i18nT('apps.specBuilder.components.specDetail.approved_by_user', { user: ap.user })
                : i18nT('apps.specBuilder.components.specDetail.approved')}
            >
              <Check size={12} strokeWidth={2.5} />
              {i18nT('apps.specBuilder.components.specDetail.approved')}
            </span>
          )
      })()}
      <span className="flex-1" />
      <Btn
        onClick={() => setExpanded(!fullscreen)}
        title={fullscreen ? i18nT('apps.specBuilder.components.specDetail.close_esc') : i18nT('apps.specBuilder.components.specDetail.expand_for_review_esc_to_close')}
        ariaLabel={fullscreen ? i18nT('apps.specBuilder.components.specDetail.close_review_view') : i18nT('apps.specBuilder.components.specDetail.expand_document_for_review')}
        label={fullscreen ? <Minimize2 className="lucide-inline" /> : <Maximize2 className="lucide-inline" />}
      />
      {/* Hidden while the overlay is up: that header hosts the same actions,
          and leaving both in the tree made getByRole find two Approves. The
          column is covered anyway. */}
      {!expanded && phaseActions()}
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
          {isMobile
            ? (
              <span className="text-[15px] font-bold tracking-tight text-text-strong overflow-hidden text-ellipsis whitespace-nowrap">
                {detail?.title || name}
              </span>
            )
            : (
              // The label is what is shown; the NAME is the identity and stays put.
              // A misnamed spec was previously unfixable without deleting it, which
              // threw away the conversation along with the name.
              <button
                type="button"
                onClick={() => setTitleDraft(detail?.title ?? '')}
                title={i18nT('apps.specBuilder.components.specDetail.rename_this_spec_label_the_folder_name_stays')}
                className="group flex items-center gap-1.5 min-w-0 bg-transparent border-none p-0 cursor-pointer focus-ring rounded"
              >
                <span className="text-[15px] font-bold tracking-tight text-text-strong overflow-hidden text-ellipsis whitespace-nowrap">
                  {detail?.title || name}
                </span>
                <Pencil size={11} strokeWidth={2} className="text-muted opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
              </button>
            )}
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
          {/* Lifecycle beyond create-and-delete. Delete used to be the only way to
              get a spec out of the rail, so tidying up and destroying the work were
              the same action. */}
          {hasIdentity && titleDraft === null && dupDraft === null && (
              <DropdownMenu>
                <DropdownMenuTrigger
                  aria-label={i18nT('apps.issueRadar.components.detailOverflowMenu.more_actions')}
                  title={i18nT('apps.issueRadar.components.detailOverflowMenu.more_actions')}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 border border-border bg-transparent text-text text-[13px] cursor-pointer font-body transition-all active:scale-[0.97] hover:border-border-strong hover:bg-bg-hover rounded-full focus-ring"
                >
                  <MoreHorizontal className="lucide-inline" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[190px]">
                  <DropdownMenuItem
                    onSelect={() => {
                      setDupDraft(null)
                      setTitleDraft(detail?.title ?? '')
                    }}
                  >
                    <Pencil size={13} className="shrink-0 text-muted" />
                    <span>{i18nT('apps.specBuilder.components.specDetail.rename_this_spec_label_the_folder_name_stays')}</span>
                  </DropdownMenuItem>
                  {detail?.duplicate_supported !== false && (
                    <DropdownMenuItem
                      disabled={duplicateMutation.isPending}
                      onSelect={() => {
                        setTitleDraft(null)
                        setDupDraft(name + '-copy')
                      }}
                    >
                      <Copy size={13} className="shrink-0 text-muted" />
                      <span>{i18nT('apps.specBuilder.components.specDetail.duplicate_this_spec')}</span>
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem
                    disabled={archiveMutation.isPending}
                    onSelect={() => archiveMutation.mutate(!detail?.archived)}
                  >
                    {detail?.archived
                      ? <ArchiveRestore size={13} className="shrink-0 text-muted" />
                      : <Archive size={13} className="shrink-0 text-muted" />}
                    <span>{detail?.archived
                      ? i18nT('apps.specBuilder.components.specDetail.restore_this_spec')
                      : i18nT('apps.specBuilder.components.specDetail.archive_this_spec')}</span>
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    aria-label={i18nT('apps.specBuilder.components.specDetail.delete_spec_named', { name })}
                    disabled={deleteMutation.isPending}
                    onSelect={() => setPendingRemove(specId())}
                  >
                    <Trash2 className="lucide-inline shrink-0 text-danger" />
                    <span className="text-danger">
                      {i18nT('apps.specBuilder.components.specDetail.delete_this_spec')}
                    </span>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
          )}
          {!isMobile && (
            <span
              className="text-[11px] font-mono text-muted overflow-hidden text-ellipsis whitespace-nowrap max-w-[45%]"
              style={{ direction: 'rtl', textAlign: 'right' }}
              title={detail?.working_dir || ''}
            >
              {detail?.working_dir || ''}
            </span>
          )}
        </header>
        {titleDraft !== null && (
          <div
            data-testid="title-form"
            className="shrink-0 flex flex-col gap-2 px-4 py-3 border-b border-border bg-bg-elevated"
          >
            <Input
              autoFocus
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitTitle()
                if (e.key === 'Escape') setTitleDraft(null)
              }}
              maxLength={120}
              placeholder={name}
              aria-label={i18nT('apps.specBuilder.components.specDetail.spec_label')}
              className="w-full min-w-0"
            />
            <div className="flex items-center justify-end gap-2">
              <Btn
                label={<X className="lucide-inline" />}
                ariaLabel={i18nT('apps.specBuilder.components.specDetail.close_esc')}
                title={i18nT('apps.specBuilder.components.specDetail.close_esc')}
                onClick={() => setTitleDraft(null)}
              />
              <Btn
                label={i18nT('apps.specBuilder.components.settingsModal.save')}
                disabled={titleMutation.isPending}
                onClick={commitTitle}
              />
            </div>
          </div>
        )}
        {dupDraft !== null && (
          <div
            data-testid="duplicate-form"
            className="shrink-0 flex flex-col gap-2 px-4 py-3 border-b border-border bg-bg-elevated"
          >
            <Input
              autoFocus
              value={dupDraft}
              onChange={(e) => setDupDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitDuplicate()
                if (e.key === 'Escape') setDupDraft(null)
              }}
              maxLength={64}
              placeholder={i18nT('apps.specBuilder.components.specDetail.name_for_the_copy')}
              aria-label={i18nT('apps.specBuilder.components.specDetail.name_for_the_copy')}
              className="w-full min-w-0"
            />
            <div className="flex items-center justify-end gap-2">
              <Btn
                label={<X className="lucide-inline" />}
                ariaLabel={i18nT('apps.specBuilder.components.specDetail.close_esc')}
                title={i18nT('apps.specBuilder.components.specDetail.close_esc')}
                onClick={() => setDupDraft(null)}
              />
              <Btn
                label={i18nT('apps.specBuilder.components.specDetail.duplicate_this_spec')}
                disabled={!dupDraft.trim() || duplicateMutation.isPending}
                onClick={commitDuplicate}
              />
            </div>
          </div>
        )}
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
                onSend={messageMutation.mutateAsync}
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
            {/* Body HIDDEN, not unmounted, while narrow: DocView holds an
                in-progress comment draft, and a remount on rotate would drop it.
                While the overlay is up the column copy is not rendered at all —
                a second MarkdownRenderer behind an opaque layer doubled parse
                cost and ran a second selection listener on the same window
                selection. */}
            <div className={`flex-1 min-h-0 flex flex-col ${isMobile ? 'hidden' : ''}`}>
              {!expanded && (
                <DocView
                  detail={detail}
                  tab={tab}
                  addComment={addComment}
                  running={running}
                  composer={docComposer}
                  runTask={runTask}
                  pendingTaskIndex={pendingTask}
                />
              )}
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
          aria-label={i18nT('apps.specBuilder.components.specDetail.review_document_named', { document: tab, name })}
          tabIndex={-1}
          ref={overlayRef}
          className="absolute inset-0 z-[60] bg-bg flex flex-col outline-none"
          style={{ padding: '14px 26px 20px' }}
        >
          <style>{DOC_CSS}</style>
          <div className="flex items-center gap-2.5 mb-2.5 shrink-0 flex-wrap">
            <span className="text-[15px] font-bold text-text-strong">{name}</span>
            <span className="text-[12px] font-mono px-2.5 py-[3px] rounded-full" style={{ color: ACCENT, background: SEL_BG }}>{i18nT('apps.specBuilder.components.specDetail.document_file_name', { name: tab })}</span>
            <span className="flex-1" />
            <SegmentedControl<DocTabId>
              segments={DOC_TABS.map((t) => ({ key: t.id, label: i18nT(t.labelKey) }))}
              value={tab}
              onChange={setTab}
              layoutId="sb-doc-tabs-overlay"
            />
            {phaseActions()}
            <Btn
              onClick={() => setExpanded(false)}
              title={i18nT('apps.specBuilder.components.specDetail.close_esc')}
              ariaLabel={i18nT('apps.specBuilder.components.specDetail.close_review_view')}
              label={<Minimize2 className="lucide-inline" />}
            />
          </div>
          <div className="flex-1 min-h-0 flex justify-center">
            <div className="sb-doc flex flex-col border border-border rounded-lg bg-card overflow-hidden min-h-0" style={{ width: 'min(980px, 100%)' }}>
              {/* The overlay is the review surface, so it carries the same editor
                  and task controls. Omitting them here would mean the fullscreen
                  view — the one with room to actually read a document — was the
                  only place you could not act on it. */}
              <DocView
                detail={detail}
                tab={tab}
                addComment={addComment}
                running={running}
                composer={docComposer}
                runTask={runTask}
                pendingTaskIndex={pendingTask}
              />
            </div>
          </div>
        </div>
      )}

      {confirmDelete && (
        <Modal
          open
          onClose={() => { if (!deleteMutation.isPending) setPendingRemove(null) }}
          title={i18nT('apps.specBuilder.components.specDetail.remove_this_spec')}
          maxWidth={440}
          footer={
            <>
              <Btn
                label={i18nT('apps.specBuilder.components.specDetail.cancel')}
                disabled={deleteMutation.isPending}
                onClick={() => setPendingRemove(null)}
              />
              <Btn
                label={deleteMutation.isPending
                  ? i18nT('apps.specBuilder.components.specDetail.removing')
                  : i18nT('apps.specBuilder.components.specDetail.delete_this_spec')}
                danger
                disabled={deleteMutation.isPending || !pendingRemove}
                onClick={() => { if (pendingRemove) deleteMutation.mutate(pendingRemove) }}
              />
            </>
          }
        >
          <p className="text-[13px] leading-relaxed text-text m-0 break-words">
            {i18nT('apps.specBuilder.components.specDetail.remove_spec_body', { name })}
          </p>
        </Modal>
      )}
    </div>
  )
}
