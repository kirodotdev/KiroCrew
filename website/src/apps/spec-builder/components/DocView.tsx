// DocView — renders one spec document via the dashboard's MarkdownRenderer.
// Selecting text raises a floating "Comment" pill; the composer is a real
// FOOTER below the scroll area (never overlaps the text). Submitting stacks the
// comment (with file attribution) into the parent's tray — nothing is sent to
// the agent until "Send all to agent".
import { useEffect, useRef, useState } from 'react'
import { MessageSquare, Plus, X, FileText, Pencil, Save, AlertTriangle } from 'lucide-react'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { Input } from '../../../components/ui'
import type { SpecDetail, SpecTask } from '../api'
import { ACCENT, SEL_BG, Btn, inputStyle } from './shared'
import { DocSkeleton } from './Shimmer'
import TaskList from './TaskList'

import { i18nT } from '../../../i18n/t'
interface Selection {
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
  /** Persist an edit. Resolves on success; rejects with the backend's code, so a
   *  `doc_conflict` can be surfaced as "reload" rather than a generic failure.
   *  Absent = editing is not offered. */
  saveDoc?: (file: string, content: string, baseHash: string) => Promise<void>
  /** Dispatch a single task. Absent = the run controls are not offered. */
  runTask?: (task: SpecTask) => void
  pendingTaskIndex?: number | null
}

export default function DocView({
  detail,
  tab,
  addComment,
  running = false,
  saveDoc,
  runTask,
  pendingTaskIndex = null,
}: DocViewProps) {
  const fname = tab + '.md'
  const content = detail?.files?.[fname]
  const docMeta = detail?.docs?.[fname]
  const boxRef = useRef<HTMLDivElement>(null)
  const [sel, setSel] = useState<Selection | null>(null)
  const [note, setNote] = useState<Selection | null>(null)
  const [draft, setDraft] = useState('')

  // ── editing ──
  // The base hash the editor OPENED against travels with the save, so a write the
  // agent made in the meantime is refused instead of silently overwritten. Held in
  // state rather than read from `detail` at submit time: this component re-renders
  // on every 2.5s poll, so reading it live would quietly re-base the edit onto the
  // agent's newer version and defeat the guard entirely.
  const [editing, setEditing] = useState<{ text: string; baseHash: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState('')
  const editable = !!saveDoc && !!docMeta?.editable

  // Leaving the document (tab switch or a different spec) drops an unsaved draft
  // rather than carrying it to the next file, where saving it would write this
  // document's text into that one.
  useEffect(() => {
    setEditing(null)
    setSaveErr('')
  }, [fname, detail?.spec_dir])

  const beginEdit = () => {
    setSaveErr('')
    setEditing({ text: content ?? '', baseHash: docMeta?.hash ?? '' })
  }

  const commit = async () => {
    if (!editing || !saveDoc) return
    setSaving(true)
    setSaveErr('')
    try {
      await saveDoc(fname, editing.text, editing.baseHash)
      setEditing(null)
    } catch (e) {
      const err = e as { code?: string; message?: string }
      setSaveErr(
        err.code === 'doc_conflict'
          ? i18nT('apps.specBuilder.components.docView.this_document_changed_while_you_were_editing')
          : err.message || i18nT('apps.specBuilder.components.docView.could_not_save_this_document'),
      )
    } finally {
      setSaving(false)
    }
  }

  const onSelectionSettled = () => {
    // Selecting inside the editor is ordinary text selection, not a review
    // gesture: raising the Comment pill over a textarea would offer to send the
    // agent feedback about a draft that is not saved anywhere yet.
    if (editing) { setSel(null); return }
    const s = window.getSelection()
    const text = s ? s.toString().replace(/\s+/g, ' ').trim() : ''
    if (!text || text.length < 3 || !boxRef.current || !s || !s.rangeCount) { setSel(null); return }
    const range = s.getRangeAt(0)
    if (!boxRef.current.contains(range.commonAncestorContainer)) { setSel(null); return }
    const r = range.getBoundingClientRect()
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

  // The tasks tab renders the checklist as work rather than as prose. Editing
  // switches it back to the raw markdown, so the file itself stays reachable.
  const showTasks = tab === 'tasks' && !editing && !!runTask && !!detail?.tasks?.length

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {/* Document toolbar. Only rendered when there is something to act on, so an
          empty or still-drafting document keeps its uncluttered empty state. */}
      {(editable || editing || docMeta?.reason === 'redacted') && (
        <div className="shrink-0 flex items-center gap-2 px-3 py-1.5 border-b border-border">
          {docMeta?.reason === 'redacted' && !editing && (
            // Not editable BECAUSE the rendering is redacted: saving it back would
            // write [redacted] over the real value. Say which, rather than showing
            // a disabled button with no explanation.
            <span className="flex items-center gap-1.5 text-[11px] text-muted">
              <AlertTriangle size={12} strokeWidth={2} />
              {i18nT('apps.specBuilder.components.docView.read_only_this_document_contains_redacted_content')}
            </span>
          )}
          {saveErr && (
            <span className="text-[11px] font-semibold flex-1 min-w-0 overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: 'var(--err)' }}>
              {saveErr}
            </span>
          )}
          <span className="flex-1" />
          {editing
            ? (
              <>
                <span className="text-[11px] text-muted">
                  {i18nT('apps.specBuilder.components.docView.unsaved_changes')}
                </span>
                <Btn
                  label={<><Save className="lucide-inline" /> {saving
                    ? i18nT('apps.specBuilder.components.docView.saving')
                    : i18nT('apps.specBuilder.components.docView.save')}</>}
                  primary
                  disabled={saving}
                  onClick={() => { void commit() }}
                />
                <Btn
                  label={i18nT('apps.specBuilder.components.docView.cancel')}
                  disabled={saving}
                  onClick={() => { setEditing(null); setSaveErr('') }}
                />
              </>
            )
            : editable && (
              <Btn
                label={<><Pencil className="lucide-inline" /> {i18nT('apps.specBuilder.components.docView.edit')}</>}
                ariaLabel={i18nT('apps.specBuilder.components.docView.edit_document', { document: tab })}
                title={i18nT('apps.specBuilder.components.docView.edit_this_document_directly')}
                onClick={beginEdit}
              />
            )}
        </div>
      )}
      <div ref={boxRef} className="flex-1 min-h-0 overflow-y-auto text-[13px] relative">
        {editing ? (
          <textarea
            autoFocus
            value={editing.text}
            onChange={(e) => setEditing({ ...editing, text: e.target.value })}
            aria-label={i18nT('apps.specBuilder.components.docView.edit_document', { document: tab })}
            spellCheck={false}
            className="w-full h-full resize-none font-mono text-[12px] leading-relaxed px-4 py-3 focus-ring"
            style={{ ...inputStyle, borderRadius: 0, border: 'none', minHeight: '100%' }}
          />
        ) : showTasks ? (
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
            <FileText size={26} strokeWidth={1.5} className="text-muted opacity-50" />
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
              onKeyDown={(e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') { setNote(null); setDraft('') } }}
              placeholder={i18nT('apps.specBuilder.components.docView.your_feedback_on_this_passage_enter_adds_it_to_t')}
              aria-label={i18nT('apps.specBuilder.components.docView.your_feedback_on_the_passage_in', { document: note.tab }) + '.md'}
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
