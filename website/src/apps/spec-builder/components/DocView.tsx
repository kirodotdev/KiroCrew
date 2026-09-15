// DocView — renders one spec document via the dashboard's MarkdownRenderer.
// Selecting text raises a floating "Comment" pill; the composer is a real
// FOOTER below the scroll area (never overlaps the text). Submitting stacks the
// comment (with file attribution) into the parent's tray — nothing is sent to
// the agent until "Send all to agent".
import { useEffect, useRef, useState } from 'react'
import { MessageSquare, Plus, X, FileText, ListChecks } from 'lucide-react'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { Input } from '../../../components/ui'
import type { SpecDetail, SpecTask } from '../api'
import { ACCENT, SEL_BG, Btn } from './shared'
import { DocSkeleton } from './Shimmer'
import TaskList from './TaskList'

import { i18nT } from '../../../i18n/t'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { containedSelectionRange } from '../../../utils/selectionContainment'

export interface Selection {
  text: string
  x: number
  y: number
  /** The document the passage was selected IN, captured at selection time. The
   *  composer stays open across a tab switch, so reading the live `tab` prop at
   *  submit time attributed the feedback to whichever document was selected
   *  LAST — the agent then received a quote that does not appear in the file it
   *  was told to fix. */
  tab: string
}

/** Lifted so SpecDetail can remount this view (column ↔ overlay) without
 *  dropping an in-progress comment. Required — both production hosts pass it. */
export interface DocComposer {
  sel: Selection | null
  setSel: (s: Selection | null) => void
  note: Selection | null
  setNote: (s: Selection | null) => void
  draft: string
  setDraft: (s: string) => void
}

/** Catalog key per document tab; a literal Record of keys is the shape
 *  check-i18n-keys.mjs resolves statically. */
const EMPTY_KEY: Record<string, string> = {
  requirements: 'apps.specBuilder.components.docView.empty_requirements',
  design: 'apps.specBuilder.components.docView.empty_design',
  tasks: 'apps.specBuilder.components.docView.empty_tasks',
}

export interface DocViewProps {
  detail: SpecDetail | null
  tab: string
  /** True while the spec's agent is working — selects the skeleton over the
   *  empty state, so an in-flight document reads as pending, not absent. */
  running?: boolean
  addComment: (c: { file: string; quote: string; note: string }) => void
  /** Lifted by SpecDetail so an overlay transition preserves a draft. */
  composer: DocComposer
  /** Dispatch a single task. Absent = the run controls are not offered. */
  runTask?: (task: SpecTask) => void
  pendingTaskIndex?: number | null
}

export default function DocView({
  detail,
  tab,
  addComment,
  running = false,
  composer,
  runTask,
  pendingTaskIndex = null,
}: DocViewProps) {
  const ime = useImeGuard()
  const fname = tab + '.md'
  const content = detail?.files?.[fname]
  const boxRef = useRef<HTMLDivElement>(null)
  const { sel, setSel, note, setNote, draft, setDraft } = composer
  const [taskDocument, setTaskDocument] = useState(false)

  const onSelectionSettled = () => {
    const s = window.getSelection()
    const text = s ? s.toString().replace(/\s+/g, ' ').trim() : ''
    if (!text || text.length < 3 || !boxRef.current || !s || !s.rangeCount) { setSel(null); return }
    const range = s.getRangeAt(0)
    // A triple-click of the document's LAST paragraph normalizes to a boundary
    // point past the pane, so ancestor containment alone would dismiss it and
    // the Comment pill would never appear (#7891). The pill is positioned from
    // the clamped range, keeping an accepted overhang's line box out of the rect.
    const contained = containedSelectionRange(range, boxRef.current)
    if (!contained) { setSel(null); return }
    const r = contained.getBoundingClientRect()
    const host = boxRef.current.getBoundingClientRect()
    setSel({ text: text.slice(0, 500), x: r.left - host.left + r.width / 2, y: r.top - host.top + boxRef.current.scrollTop, tab })
  }

  const submit = () => {
    if (!draft.trim() || !note) return
    addComment({ file: note.tab + '.md', quote: note.text, note: draft.trim() })
    setNote(null); setDraft(''); setSel(null)
  }

  // Selection is detected on the container via listeners rather than a JSX
  // handler so KEYBOARD selection (Shift+Arrow, Shift+Home/End) raises the
  // Comment pill too — a mouseup-only handler would leave keyboard users
  // unable to reach the review affordance at all.
  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    el.addEventListener('mouseup', onSelectionSettled)
    el.addEventListener('keyup', onSelectionSettled)
    return () => {
      el.removeEventListener('mouseup', onSelectionSettled)
      el.removeEventListener('keyup', onSelectionSettled)
    }
  })

  const hasTaskControls = tab === 'tasks' && !!runTask && !!detail?.tasks?.length
  const showTasks = hasTaskControls && !taskDocument

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {hasTaskControls && (
        <div className="shrink-0 flex items-center gap-2 px-4 py-2 border-b border-border">
          <Btn
            primary={!taskDocument}
            onClick={() => setTaskDocument(false)}
            label={<><ListChecks className="lucide-inline" /> {i18nT('apps.specBuilder.components.taskList.task_progress')}</>}
          />
          <Btn
            primary={taskDocument}
            onClick={() => setTaskDocument(true)}
            label={<><FileText className="lucide-inline" /> {i18nT('apps.specBuilder.components.docView.document_file_name', { name: 'tasks' })}</>}
          />
        </div>
      )}
      <div ref={boxRef} className="flex-1 min-h-0 overflow-y-auto text-[13px] relative">
        {showTasks ? (
          <TaskList
            tasks={detail?.tasks ?? []}
            progress={detail?.task_progress}
            pendingIndex={pendingTaskIndex}
            busy={running || detail?.status === 'executing'}
            onRun={(t) => runTask?.(t)}
          />
        ) : content ? (
          <div className="px-5 py-[18px]">
            <MarkdownRenderer content={content} />
          </div>
        ) : running ? (
          // The agent is actively writing this file: hold the document's shape
          // with a skeleton (Issue Radar's layout-continuity pattern) instead of
          // a spinner, so the pane doesn't jump when the text lands.
          <DocSkeleton />
        ) : (
          // Centred, icon-paired empty state filling the pane. A left-aligned
          // sentence pinned to the top-left read as a glitch — the same fix
          // Issue Radar's ListEmptyState made for its columns.
          <div className="h-full flex flex-col items-center justify-center gap-2.5 text-center px-6">
            <FileText strokeWidth={1.5} className="lucide-inline text-muted opacity-50" />
            <div className="text-[13px] text-muted max-w-[420px] leading-relaxed">
              {Object.prototype.hasOwnProperty.call(EMPTY_KEY, tab)
                ? i18nT(EMPTY_KEY[tab])
                : i18nT('apps.specBuilder.components.docView.nothing_here_yet')}
            </div>
          </div>
        )}
        {sel && !note && (
          <div
            className="absolute z-[5]"
            style={{
              left: Math.max(8, Math.min(sel.x - 44, 600)),
              top: Math.max(4, sel.y - 34),
            }}
          >
            <Btn
              primary
              onClick={() => { setNote(sel); setSel(null) }}
              ariaLabel={i18nT('apps.specBuilder.components.docView.comment_on_the_selected_passage')}
              label={<><MessageSquare className="lucide-inline" /> {i18nT('apps.specBuilder.components.docView.comment')}</>}
            />
          </div>
        )}
      </div>
      {note && (
        <div className="shrink-0 bg-card px-3.5 py-2.5" style={{ borderTop: '2px solid ' + ACCENT }}>
          <div className="flex items-center gap-2 mb-[7px]">
            <span className="text-[11px] font-bold px-2 py-0.5 rounded-full shrink-0" style={{ color: ACCENT, background: SEL_BG }}>{i18nT('apps.specBuilder.components.docView.document_file_name', { name: note.tab })}</span>
            <span
              className="text-[11px] text-muted pl-2 overflow-hidden text-ellipsis whitespace-nowrap flex-1"
              style={{ borderLeft: '3px solid ' + ACCENT }}
            >
              “{note.text.slice(0, 140)}{note.text.length > 140 ? '…' : ''}”
            </span>
          </div>
          <div className="flex gap-2">
            <Input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              {...ime.bindEnter({ onEnter: submit, onEscape: () => { setNote(null); setDraft('') } })}
              placeholder={i18nT('apps.specBuilder.components.docView.your_feedback_on_this_passage_enter_adds_it_to_t')}
              aria-label={i18nT('apps.specBuilder.components.docView.your_feedback_on_the_passage_in', { document: note.tab + '.md' })}
              className="flex-1"
            />
            <Btn label={<><Plus className="lucide-inline" /> {i18nT('apps.specBuilder.components.docView.add_comment')}</>} primary disabled={!draft.trim()} onClick={submit} />
            <Btn label={<X className="lucide-inline" />} ariaLabel={i18nT('apps.specBuilder.components.docView.discard_this_comment')} onClick={() => { setNote(null); setDraft('') }} />
          </div>
        </div>
      )}
    </div>
  )
}
