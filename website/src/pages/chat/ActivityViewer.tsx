import { useEffect, useRef, useState, useCallback, useMemo, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Bot, ScrollText, FileText, X, Lock, CheckCircle, AlertCircle, Loader as LoaderIcon, Ban, Handshake, Wrench, MessageSquare, Workflow, Star, Component, GitPullRequest, ArrowLeft, Square, RotateCcw } from 'lucide-react'
import { api } from '../../api/client'
import MarkdownPanel, { type MarkdownPanelHandle } from '../../components/MarkdownPanel'
import { fileReadUrl } from '../../utils/fileReadUrl'
import { LogViewer } from '../LogsPage'
import TrustDropdown from '../../components/TrustDropdown'
import Clickable from '../../components/Clickable'
import type { SubagentActivity, ToolActivity, SessionDoc, Artifact } from '../../types'
import type { TouchedFile } from '../../hooks/useTouchedFiles'
import { getInlineDraft, setInlineDraft, clearInlineDraft } from '../../hooks/usePanelTabs'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import { dedupResourceLinks, resourceKey } from '../../utils/extractChatLinks'
import type { PullRequestLink } from '../../utils/pullRequestLinks'
import PullRequestPanel from '../../components/PullRequestPanel'
import { useAppSelector, useAppDispatch } from '../../store'
import { markSubagentApproving, openActivityToTab, selectSubagent, clearTerminalSubagents } from '../../store/chatSlice'
import SegmentedControl from '../../components/SegmentedControl'
import { colorForExt, fileIcon } from '../../utils/fileIcons'
import SideChat from './SideChat'
import WorkflowSidebarRow, { type WfRunRow } from './WorkflowSidebarRow'
import { runBelongsToSlot } from '../../apps/workflows/runModel'

const STATUS = {
  pending: <Lock size={12} className="text-muted" />,
  running: <LoaderIcon size={12} className="text-accent animate-spin" />,
  tool: <Wrench size={12} className="text-amber-400" />,
  done: <CheckCircle size={12} className="text-green-400" />,
  error: <AlertCircle size={12} className="text-danger" />,
  stopped: <Square size={12} className="text-muted" />,
} as const

// Keyed by extractChatLinks' LinkType, which is 'cr' | 'other' only — the OSS
// fork classifies URLs as generic git PR/review vs everything else. Use design
// tokens (not hardcoded Tailwind palette colors) so both themes stay consistent.
const RESOURCE_TYPE_COLORS: Record<string, string> = {
  cr: 'bg-accent-subtle text-accent',
  other: 'bg-muted/15 text-muted',
}

const RESOURCE_TYPE_LABELS: Record<string, string> = {
  cr: 'PR',
  other: 'Link',
}

function fmtTime(ts: number) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/* ── Subagent pane ── */

/** Lazy-load subagent output from disk on demand (memory-friendly).
 *  Backend GET /api/spawn/{id} applies _redact() (redact_exfiltration_urls + redact_credentials)
 *  — see messaging.py:api_spawn_status line 109. */
function DiskLoader({ id, autoLoad }: { id: string; autoLoad?: boolean }) {
  const [text, setText] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const ctrlRef = useRef<AbortController | null>(null)
  useEffect(() => () => { ctrlRef.current?.abort() }, [])
  const load = useCallback(() => {
    ctrlRef.current?.abort()
    const ctrl = ctrlRef.current = new AbortController()
    setLoading(true); setError(false)
    api.spawnStatus(id, { signal: ctrl.signal })
      .then(d => { if (!ctrl.signal.aborted) setText(d.result || '(no output)') })
      .catch(() => { if (!ctrl.signal.aborted) setError(true) })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false) })
  }, [id])
  // 1-click transcript: a chip-selected card loads its output immediately
  // instead of waiting for the manual button press.
  useEffect(() => {
    if (autoLoad && text === null && !loading && !error) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoLoad])
  if (text !== null) return <>{text}</>
  if (loading) return <span className="text-muted/30 italic">Loading…</span>
  if (error) return <button className="text-danger/70 hover:text-danger text-[12px] underline cursor-pointer bg-transparent border-none p-0 font-mono" onClick={e => { e.stopPropagation(); load() }}>Failed — click to retry</button>
  return <button className="text-accent/70 hover:text-accent text-[12px] underline cursor-pointer bg-transparent border-none p-0 font-mono" onClick={e => { e.stopPropagation(); load() }}>Load output from disk</button>
}

function SubagentPane({ a, onClick, selected }: { a: SubagentActivity; onClick: () => void; selected?: boolean }) {
  const bodyRef = useRef<HTMLPreElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const autoScroll = useRef(true)
  const isPending = a.status === 'pending'
  const isDone = a.status === 'done' || a.status === 'error' || a.status === 'stopped'
  // Native cards have no SubagentManager record to lazy-load from disk; their
  // output arrives inline on the done event (a.result).
  const isNative = a.id.startsWith('native:')
  const [collapsed, setCollapsed] = useState(isDone)
  // Auto-collapse when transitioning to done (not on mount)
  const wasDone = useRef(isDone)
  useEffect(() => {
    if (isDone && !wasDone.current) { const t = setTimeout(() => setCollapsed(true), 2000); wasDone.current = true; return () => clearTimeout(t) }
  }, [isDone])
  const isRunning = a.status === 'running' || a.status === 'tool'

  // Approval handling for pending subagents
  const dispatch = useAppDispatch()
  // 1-click transcript: chip selection expands the card, scrolls it into
  // view, and (via DiskLoader autoLoad) fetches the output — then clears the
  // selection so a later re-click re-triggers.
  useEffect(() => {
    if (!selected) return
    setCollapsed(false)
    cardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    const t = setTimeout(() => dispatch(selectSubagent(null)), 800)
    return () => clearTimeout(t)
  }, [selected, dispatch])
  const onApprove = useCallback((e: React.MouseEvent, action: 'approve' | 'reject') => {
    e.stopPropagation()
    if (!a.approval_id) return
    dispatch(markSubagentApproving({ id: a.id, approving: true }))
    api.resolveApproval(a.approval_id, action).catch(() => dispatch(markSubagentApproving({ id: a.id, approving: false })))
  }, [a.approval_id, a.id, dispatch])

  // Live elapsed timer for running subagents
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!isRunning) return
    const tick = () => setElapsed(Math.floor((Date.now() - a.startedAt) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [isRunning, a.startedAt])

  useEffect(() => {
    const el = bodyRef.current
    if (el && autoScroll.current) el.scrollTop = el.scrollHeight
  }, [a.streaming, a.lastTool])

  const onScroll = useCallback(() => {
    const el = bodyRef.current
    if (!el) return
    autoScroll.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 20
  }, [])

  const onCancel = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    api.spawnDelete(a.id).catch(() => {})
  }, [a.id])

  const displayElapsed = isRunning ? elapsed : Math.round(a.elapsed || 0)
  const fmtElapsed = displayElapsed >= 60 ? `${Math.floor(displayElapsed / 60)}m ${displayElapsed % 60}s` : `${displayElapsed}s`

  // Inside the Subagents tab the "Subagent" prefix is redundant, and in a
  // narrow rail it was the part that survived truncation while the actual
  // status got clipped. Show the status; keep the full phrase as the tooltip.
  const statusLabel = isPending
    ? 'Pending Approval'
    : a.status === 'tool' ? 'Running Tool'
      : a.status === 'running' ? (a.streaming ? 'Running' : 'Starting…')
        : a.status === 'done' ? 'Complete'
          : a.status === 'stopped' ? 'Stopped'
            : a.error?.includes('Cancelled') ? 'Cancelled' : 'Error'

  return (
    // Card-level mouse convenience that selects the subagent; it wraps its own
    // interactive controls (Cancel, collapse header) which carry the real
    // keyboard/AT semantics. The outer div carries the scroll-to anchor for
    // chip-selected cards.
    <div ref={cardRef}>
    <Clickable className={`mx-2 mb-3 rounded-lg border bg-card overflow-hidden shadow-sm transition-all animate-scale-in ${isRunning || isPending ? 'border-border-strong' : 'border-border opacity-60'}${selected ? ' ring-1 ring-accent' : ''}`} onClick={onClick}>
      {/* Header — collapse toggle when the subagent is done */}
      <div
        className={`flex items-center gap-2 px-3 py-2.5${isDone ? ' cursor-pointer select-none hover:bg-bg-hover transition-colors' : ''}`}
        {...(isDone
          ? {
              role: 'button' as const,
              tabIndex: 0,
              'aria-expanded': !collapsed,
              onClick: () => setCollapsed(c => !c),
              onKeyDown: (e: React.KeyboardEvent) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setCollapsed(c => !c) }
              },
            }
          : {})}
      >
        <span className="shrink-0 flex items-center">{STATUS[a.status]}</span>
        <span className="text-[13px] font-semibold text-text truncate min-w-0" title={`Subagent ${statusLabel}`}>{statusLabel}</span>
        {a.agent && <code className="text-[11px] text-muted/50 bg-bg-hover px-1.5 py-0.5 rounded shrink-[3] min-w-0 max-w-[6.5rem] truncate inline-block align-middle" title={a.agent}>{a.agent}</code>}
        {!isPending && <span className="text-[11px] text-muted/40 ml-auto font-mono shrink-0 whitespace-nowrap tabular-nums">{fmtElapsed}</span>}
        {isRunning && <button data-testid="subagent-cancel-btn" className="text-[11px] px-1.5 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all shrink-0 whitespace-nowrap inline-flex items-center" onClick={onCancel}><X className="lucide-inline" /> Cancel</button>}
        {isDone && <span className="text-[14px] text-muted bg-bg-hover px-1.5 py-0.5 rounded shrink-0 ml-1">{collapsed ? '▸' : '▾'}</span>}
      </div>
      {/* Input (task) */}
      {!collapsed && (
        <div className="px-3 pt-1 pb-2">
          <div className="text-[10px] text-muted/40 uppercase tracking-wider mb-1">Input</div>
          <pre className="px-2.5 py-2 bg-bg rounded-md text-[12px] font-mono whitespace-pre-wrap break-all max-h-[120px] overflow-y-auto text-muted/80 leading-relaxed">{a.task}</pre>
        </div>
      )}
      {/* Approval buttons for pending */}
      {isPending && !a.approving && (
        <div className="px-3 pb-2 flex gap-1.5">
          <button className="px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all" onClick={e => onApprove(e, 'approve')}><CheckCircle className="lucide-inline" /> Approve</button>
          <button className="px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all" onClick={e => onApprove(e, 'reject')}><Ban className="lucide-inline" /> Reject</button>
        </div>
      )}
      {isPending && a.approving && <div className="px-3 pb-2 text-[12px] text-muted/50">Resolving…</div>}
      {/* Output (streaming body) */}
      {!isPending && !collapsed && (
      <>
      <div className="px-3 pb-2">
        <div className="text-[10px] text-muted/40 uppercase tracking-wider mb-1">Output</div>
        <pre ref={bodyRef} onScroll={onScroll} className="px-2.5 py-2 bg-bg rounded-md text-[12px] font-mono whitespace-pre-wrap break-all max-h-[240px] overflow-y-auto text-muted/80 leading-relaxed">
          {a.streaming || a.result || (isDone ? (isNative ? <span className="text-muted/30 italic">(output shown in chat)</span> : <DiskLoader id={a.id} autoLoad={selected} />) : <span className="text-muted/30 italic">Waiting for output…</span>)}
          {a.lastTool && <div className="text-accent mt-1"><Wrench className="lucide-inline" /> {a.lastTool}</div>}
        </pre>
      </div>
      {/* Error details */}
      {a.error && (
        <div className="px-3 py-1.5 text-[12px] border-t border-border/20 space-y-0.5">
          <div className="text-red-400">{a.error}</div>
          {a.lastTool && <div className="text-muted/40">Last tool: {a.lastTool}</div>}
        </div>
      )}
      </>
      )}
    </Clickable>
    </div>
  )
}

/* ── Tool entries are now rendered inline inside chat messages (see ToolCallLine.tsx).
 *    The activity viewer only hosts subagents, logs, and the file browser. ── */

const isSpawnApproval = (e: ToolActivity) => (e.type === 'approval' || e.type === 'approval_resolved') && e.approval_type != null && e.approval_type !== 'chat'

/* ── Approval entry ── */

function ApprovalEntry({ entry, slot }: { entry: ToolActivity; slot: string }) {
  const resolved = entry.type === 'approval_resolved'
  const [localDecision, setLocalDecision] = useState<string | null>(null)
  const isResolved = resolved || !!localDecision
  const [acting, setActing] = useState(false)
  const onAction = useCallback(async (action: string, pattern?: string) => {
    setActing(true)
    setLocalDecision(action)
    try {
      if (entry.approval_type === 'chat') {
        const extra: Record<string, string> = {}
        if (entry.approval_id) extra.request_id = entry.approval_id
        if (pattern) extra.pattern = pattern
        await api.approveChatSlot(slot, action, extra)
      } else {
        await api.resolveApproval(entry.approval_id!, action === 'rejected' ? 'reject' : 'approve')
      }
    } catch { setLocalDecision(null); setActing(false) }
  }, [entry.approval_id, entry.approval_type, slot])

  const toolTitle = entry.text || ''
  const isShell = toolTitle.startsWith('Running: ')
  const normalized = toolTitle.replace(/^(Running: |Reading )/, '')
  const baseCmd = normalized.split(/\s+/)[0] || normalized

  const decisionLabel: Record<string, ReactNode> = { approved: <><CheckCircle className="lucide-inline" /> Approved</>, trust: <><Handshake className="lucide-inline" /> Trusted</>, trust_command: <><CheckCircle className="lucide-inline" /> Trusted command</>, trust_base: <><CheckCircle className="lucide-inline" /> Trusted base</>, rejected: <><Ban className="lucide-inline" /> Rejected</> }
  const btnClass = 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all'
  return (
    <div className={`mx-2 mb-2 rounded-lg border overflow-hidden shadow-sm transition-all ${isResolved ? 'border-ok/40 bg-card' : 'border-warn/40 bg-warn/5'}`}>
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="shrink-0 flex items-center">{isResolved ? <CheckCircle size={15} className="text-green-400" /> : <Lock size={15} className="text-muted" />}</span>
        <span className="text-[13px] font-semibold text-text truncate min-w-0">{isResolved ? (decisionLabel[localDecision || ''] || 'Resolved') : 'Approval Needed'}</span>
        <span className="text-[11px] text-muted/40 font-mono ml-auto shrink-0">{fmtTime(entry.ts)}</span>
      </div>
      {!isResolved && <div className="px-3 pb-2 text-[13px] text-muted/70">{entry.text}</div>}
      {!isResolved && !acting && (
        <div className="px-3 pb-2 flex gap-1.5">
          <button className={btnClass} onClick={() => onAction('approved')}><CheckCircle className="lucide-inline" /> Approve</button>
          <TrustDropdown
            fullCommand={normalized}
            baseCommand={baseCmd}
            isShell={isShell}
            className={btnClass}
            onAction={(action, pattern) => onAction(action, pattern)}
          />
          <button className={btnClass + ' hover:!text-danger hover:!border-danger'} onClick={() => onAction('rejected')}><Ban className="lucide-inline" /> Reject</button>
        </div>
      )}
      {acting && <div className="px-3 pb-2 text-[12px] text-muted/50">Resolving…</div>}
    </div>
  )
}

/* ── Files-tab inline file preview ──────────────────────────────────────────
 * Opening a file from the Files tab keeps it IN the Files tab (no new document
 * tab in the strip): the list is replaced by the file's content plus a "Back to
 * files" bar. Content is fetched here (same file-read query key as ChatPage's
 * tab opener, so re-opening is cache-instant) and rendered through the shared
 * embedded MarkdownPanel — identical viewer to the document-tab path, just
 * hosted inline. Back returns to the list. */

function FilePreview({ path, slot, onBack, onFileSave, onSubmitComments }: {
  path: string
  slot: string
  onBack: () => void
  onFileSave: (filePath: string, content: string) => Promise<void>
  onSubmitComments?: (message: string) => void
}) {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['file-read', path],
    // Same query key + result shape ({ text, ok }) as ChatPage's document-tab
    // reader, so the inline view SHARES that cache instead of colliding with it.
    queryFn: async () => {
      try {
        const res = await fetch(fileReadUrl(path))
        const text = res.ok
          ? await res.text()
          : res.status === 404 ? '_File not found on disk. It may have been moved or deleted._'
            : '_Unable to read file._'
        return { text, ok: res.ok }
      } catch {
        // Network-level failure (fetch rejected) — return a NOT-ok result rather
        // than throwing, so `data` is always defined and the editor is never
        // mounted over an empty buffer that a save could write to the file.
        return { text: '_Unable to read file._', ok: false }
      }
    },
    staleTime: 10_000,
  })
  // Working copy is backed by the module-level inline-draft store (keyed by
  // path), NOT component state, so an in-progress edit survives everything that
  // unmounts this subtree — the close control, an activity-tab switch, a chat-
  // slot switch, and the automatic force-collapse on window resize — matching
  // how document-tab content persists above the panel. On (re)open we restore a
  // preserved draft if present, else seed once from a successful disk read
  // (never the failure placeholder). One draft per path = one editor per path.
  const [content, setContentState] = useState<string>(() => getInlineDraft(slot, path) ?? '')
  // Keep the working copy synced to the freshest SUCCESSFUL disk read UNTIL the
  // user starts editing (a draft exists for this path). This avoids locking the
  // editor onto a stale (≤10s) cached read when the file changed on disk since;
  // once the user has a draft we stop syncing so their edits aren't clobbered.
  useEffect(() => {
    if (data?.ok && getInlineDraft(slot, path) === undefined) {
      setContentState(prev => (prev === data.text ? prev : data.text))
    }
  }, [data, path, slot])
  const setContent = useCallback((c: string) => {
    setContentState(c)
    setInlineDraft(slot, path, c)
  }, [slot, path])
  // Only mount the editable panel once the working copy is RECONCILED with the
  // source of truth: either the user has a draft (their edits), or the content
  // equals the successful disk read. This defers the editor past the brief
  // window where `content` is still the initial '' (or a not-yet-synced value)
  // while `data.ok` is already true from cache — mounting then would show an
  // empty/dirty buffer whose save could truncate the file.
  const inlineReady = getInlineDraft(slot, path) !== undefined || (!!data?.ok && content === data.text)
  const name = path.split('/').pop() || path
  // Keep the shared ['file-read', path] cache coherent after a save (otherwise a
  // reopen within the 10s stale window seeds pre-save content and a subsequent
  // edit could clobber the newer file), and drop the now-committed draft. Wraps
  // — never replaces — the caller's save.
  const qc = useQueryClient()
  const handleSave = useCallback(async (p: string, c: string) => {
    await onFileSave(p, c)
    qc.setQueryData(['file-read', p], { text: c, ok: true })
    // Draft reconciliation (clearing) is owned by ChatPage.handleFileSave, which
    // clears only if the draft still equals what was saved — so edits typed
    // during a pending save aren't dropped. We don't clear here.
  }, [onFileSave, qc])
  // "Back to files" reuses MarkdownPanel's existing close guard (via the
  // imperative handle) so leaving with unsaved edits shows its normal discard
  // prompt. guardedClose only calls this after the guard accepts (not dirty, or
  // the user confirmed discard), so it is safe to drop the draft here — a
  // confirmed discard should not survive to the next open. (An involuntary
  // unmount never reaches this path, so the draft is preserved there.)
  const handleClose = useCallback(() => { clearInlineDraft(slot, path); onBack() }, [slot, path, onBack])
  const panelRef = useRef<MarkdownPanelHandle>(null)
  const back = useCallback(() => {
    if (panelRef.current) { panelRef.current.requestClose(); return }
    // No editor mounted (e.g. the read failed, so the retry state is showing
    // instead of MarkdownPanel) — its close guard can't fire. If an unsaved
    // draft exists, confirm before discarding it ourselves; otherwise just go
    // back. (The guarded path above already prompts, so this never double-asks.)
    if (getInlineDraft(slot, path) !== undefined && !window.confirm('Discard unsaved changes?')) return
    handleClose()
  }, [slot, path, handleClose])

  return (
    <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
      {/* Back-to-list bar — mirrors the file's tab-chip identity so the Files
          tab reads as one place that swaps between list and file. */}
      <div className="flex items-center gap-2 h-[38px] px-2 shrink-0 border-b border-border">
        <button
          onClick={back}
          className="flex items-center gap-1.5 h-7 px-2 rounded-md text-[12px] text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
          title="Back to files"
          aria-label="Back to files"
        >
          <ArrowLeft size={14} />
          <span>Files</span>
        </button>
        <span aria-hidden="true" className="w-px h-4 bg-border shrink-0" />
        <span className="flex items-center gap-1.5 min-w-0 text-[12px] text-text-strong">
          <FileText size={13} className="text-muted shrink-0" />
          <span className="truncate" title={path}>{name}</span>
        </span>
      </div>
      <div className="flex-1 min-h-0 relative">
        {isLoading || (data?.ok && !inlineReady) ? (
          <div className="flex items-center justify-center h-full text-muted text-[13px]">Loading…</div>
        ) : data?.ok ? (
          <MarkdownPanel
            ref={panelRef}
            embedded
            filePath={path}
            content={content}
            onContentChange={setContent}
            onSave={handleSave}
            onClose={handleClose}
            savedBaseline={data?.ok ? data.text : undefined}
            onSubmitComments={onSubmitComments}
          />
        ) : (
          // Loading finished but the read did NOT succeed (404, HTTP error, or a
          // network-level rejection → `data` may be undefined). Never mount an
          // editable panel here: a save would write empty/placeholder content
          // over the real (or temporarily-unreadable) file. Offer a retry.
          <div className="flex flex-col items-center justify-center h-full gap-3 text-center px-6">
            <span className="text-[13px] text-muted">
              {data?.text ? data.text.replace(/^_|_$/g, '') : 'Unable to read this file.'}
            </span>
            <button
              onClick={() => refetch()}
              className="h-7 px-3 rounded-md text-[12px] text-text border border-border hover:bg-bg-hover transition-colors bg-transparent cursor-pointer"
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Main component ── */


export function countDiffStats(diff: string): { added: number; removed: number } {
  let added = 0, removed = 0
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++
    else if (line.startsWith('-') && !line.startsWith('---')) removed++
  }
  return { added, removed }
}

function FileTile({ f, onFileOpen, onFileRemove }: { f: TouchedFile; onFileOpen?: (p: string) => void; onFileRemove?: (p: string) => void }) {
  const name = f.path.split('/').pop() || f.path
  const Icon = fileIcon(f.path)
  const colorCls = colorForExt(f.path)
  const { data } = useQuery({
    queryKey: ['file-diff', f.path, f.lastWrite],
    queryFn: () => api.fileDiff(f.path),
    placeholderData: (prev) => prev,
  })
  const stats = data?.diff ? countDiffStats(data.diff) : null
  return (
    <div
      className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-border bg-bg-elevated text-[12px] cursor-pointer hover:bg-bg-hover hover:border-border-strong transition-all max-w-full"
      onClick={() => onFileOpen?.(f.path)}
      title={f.path}
      role="button"
      tabIndex={0}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onFileOpen?.(f.path) } }}
    >
      <span className="group/icon relative inline-flex items-center justify-center w-4 h-4 shrink-0">
        <Icon size={12} className={`${colorCls} ${onFileRemove ? 'group-hover/icon:opacity-0' : ''} transition-opacity`} />
        {onFileRemove && (
          <button
            className="absolute inset-0 flex items-center justify-center opacity-0 group-hover/icon:opacity-100 transition-opacity text-danger cursor-pointer bg-transparent border-none p-0"
            onClick={e => { e.stopPropagation(); onFileRemove(f.path) }}
            title="Remove"
            aria-label="Remove file from list"
          >
            <X size={12} />
          </button>
        )}
      </span>
      <span className="truncate text-text max-w-[140px]">{name}</span>
      {stats && (stats.added > 0 || stats.removed > 0) && (
        <span className="flex items-center gap-1 text-[10px] font-mono shrink-0 ml-0.5">
          {stats.added > 0 && <span className="text-ok">+{stats.added}</span>}
          {stats.removed > 0 && <span className="text-danger">-{stats.removed}</span>}
        </span>
      )}
    </div>
  )
}

/* ── SessionArtifactsTab ─────────────────────────────────────────────────────
 *
 * Everything this session produced, from TWO inputs:
 *
 *  1. Real artifacts scoped by `?session=` — including every `<mcwidget>` the
 *     agent emitted, which the backend auto-registers unpinned
 *     (kiro_crew/widget_artifacts.py). These have no filesystem path: a widget's
 *     HTML lives inline in the message, which is exactly why the file-backed
 *     scan below can never see them.
 *  2. Virtual session documents — non-code files recorded in chat
 *     `file_changes`, not persisted until starred (materialized).
 *
 * Both render as one list; the star means "keep in library" for either. An
 * artifact row opens the artifact, a document row opens the file.
 */
type SessionArtifactRow =
  | { kind: 'artifact'; key: string; name: string; sub: string; slug: string; starred: boolean }
  | { kind: 'doc'; key: string; name: string; sub: string; path: string; slug: string; starred: boolean }

function SessionArtifactsTab({ slot, onFileOpen }: { slot: string; onFileOpen?: (path: string) => void }) {
  const qc = useQueryClient()
  // Artifact rows have no filesystem path, so `onFileOpen` can't serve them.
  // Route to the standalone artifact page, matching the command palette's
  // Artifacts provider.
  const navigate = useNavigate()
  const { data, isFetching } = useQuery<{ docs: SessionDoc[] }>({
    queryKey: ['session-artifacts', slot],
    queryFn: () => api.artifactSessionDocs(slot),
    enabled: !!slot,
  })
  const { data: artifactData, isFetching: artifactsFetching } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['session-artifact-records', slot],
    queryFn: () => api.artifacts({ session: slot }),
    enabled: !!slot,
  })
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['session-artifacts', slot] })
    qc.invalidateQueries({ queryKey: ['session-artifact-records', slot] })
    qc.invalidateQueries({ queryKey: ['artifacts'] })
    qc.invalidateQueries({ queryKey: ['artifact-session-docs'] })
  }
  const saveMut = useMutation({ mutationFn: (path: string) => api.materializeArtifact(path, slot), onSuccess: invalidate })
  const pinMut = useMutation({
    mutationFn: ({ slug, pinned }: { slug: string; pinned: boolean }) => api.setArtifactPinned(slug, pinned),
    onSuccess: invalidate,
  })
  const busyPath = saveMut.isPending ? (saveMut.variables as string) : null
  const busySlug = pinMut.isPending ? (pinMut.variables as { slug: string }).slug : null

  const rows = useMemo<SessionArtifactRow[]>(() => {
    const artifacts = artifactData?.artifacts || []
    // A materialized document is BOTH a session doc and a real artifact; keep the
    // path-aware doc row (only it can open the file) and drop the artifact twin.
    //
    // Matching on slug alone is not enough: the session-docs backend builds its
    // path→slug map from PINNED artifacts only, so a doc that was materialized
    // and then UN-starred reports `slug: ''` and its artifact would slip through
    // as a second row with its own star. Excluding `source_path` artifacts covers
    // both states — every materialized artifact is file-backed by construction
    // (materialize requires a recorded `file_changes` entry), and an inline
    // widget never has a source_path.
    const docSlugs = new Set((data?.docs || []).map(d => d.slug).filter(Boolean))
    const out: SessionArtifactRow[] = artifacts
      .filter(a => !docSlugs.has(a.slug) && !a.source_path)
      .map(a => ({
        kind: 'artifact' as const,
        key: `artifact:${a.slug}`,
        name: a.name || a.slug,
        sub: a.kind,
        slug: a.slug,
        starred: !!a.pinned,
      }))
    for (const d of data?.docs || []) {
      out.push({
        kind: 'doc' as const,
        key: `doc:${d.path}`,
        name: d.name,
        sub: d.path,
        path: d.path,
        slug: d.slug,
        starred: d.saved,
      })
    }
    return out
  }, [artifactData, data])

  const loading = isFetching || artifactsFetching

  return (
    <div className="flex-1 overflow-y-auto py-2">
      {rows.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-muted text-[13px] py-8">{loading ? 'Loading…' : 'Nothing produced in this session yet'}</div>
      ) : (
        <div className="px-3 flex flex-col gap-0.5">
          {rows.map(r => {
            const busy = (r.kind === 'doc' && busyPath === r.path) || (!!r.slug && busySlug === r.slug)
            return (
              <div key={r.key} className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-bg-hover transition-colors">
                <button
                  type="button"
                  onClick={() => { if (r.kind === 'doc') onFileOpen?.(r.path); else navigate(`/artifacts/${r.slug}`) }}
                  className="flex items-center gap-2 min-w-0 flex-1 text-left bg-transparent border-none cursor-pointer p-0"
                  title={r.kind === 'doc' ? 'Open in side panel' : 'Open artifact'}
                >
                  {r.kind === 'doc'
                    ? <FileText size={14} className="text-emerald-400 shrink-0" />
                    : <Component size={14} className="text-accent shrink-0" />}
                  <span className="min-w-0 flex-1">
                    <span className="block text-[13px] text-text truncate">{r.name}</span>
                    <span className="block text-[11px] text-muted truncate">{r.sub}</span>
                  </span>
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    // A doc with no slug isn't persisted yet — starring it
                    // materializes it. Everything else is a metadata pin flip.
                    if (r.kind === 'doc' && !r.slug) saveMut.mutate(r.path)
                    else if (r.slug) pinMut.mutate({ slug: r.slug, pinned: !r.starred })
                  }}
                  className={`shrink-0 p-1 rounded transition-colors bg-transparent border-none cursor-pointer disabled:cursor-default ${r.starred ? 'text-accent' : 'text-muted/50 hover:text-accent'}`}
                  title={r.starred ? 'Remove star' : 'Star'}
                  aria-label={r.starred ? `Unstar ${r.name}` : `Star ${r.name}`}
                  aria-pressed={r.starred}
                >
                  {busy ? <LoaderIcon size={13} className="animate-spin" /> : <Star size={13} className={r.starred ? 'fill-current' : ''} />}
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default function ActivityViewer({ subagents, toolLog, open, onToggle, slot, files, onFileOpen, onFileRemove, navLinks, navResolving, view, sources, selectedSourceUrl, onSelectSource, onAddToChat, onFileSave, onSubmitComments, openDocPaths, previewPath, onPreviewPathChange }: {
  subagents: Record<string, SubagentActivity>; toolLog: ToolActivity[]; open: boolean; onToggle: () => void; slot: string
  files?: TouchedFile[]; onFileOpen?: (path: string) => void; onFileRemove?: (path: string) => void; onFilesClear?: (source: 'history' | 'tool') => void
  projectDir?: string
  navLinks?: ExtractedLink[]; navResolving?: boolean
  sources?: PullRequestLink[]; selectedSourceUrl?: string; onSelectSource?: (url: string) => void; onAddToChat?: (text: string) => void
  /** Save handler for the Files-tab inline file preview (opening a file keeps
   *  it in the Files tab instead of spawning a document tab). */
  onFileSave?: (filePath: string, content: string) => Promise<void>
  onSubmitComments?: (message: string) => void
  /** Absolute paths already open as `file:` document tabs. Enforces one editor
   *  per path: opening such a path from the Files list routes to its existing
   *  document tab instead of spawning a second (inline) editor for it. */
  openDocPaths?: Set<string>
  /** Files-tab inline preview path — lifted to ChatPage (survives panel
   *  collapse and lets chat-link opens route to this editor). `onPreviewPathChange`
   *  is the setter. */
  previewPath?: string | null
  onPreviewPathChange?: (path: string | null) => void
  /** When set, render ONLY this view and hide the internal SegmentedControl.
   *  Used by SidePanel, which owns the top-level tab strip. */
  view?: 'changes' | 'subagents' | 'logs' | 'files' | 'artifacts' | 'side' | 'workflows'
}) {
  const dispatch = useAppDispatch()
  const [, setSelected] = useState(0)
  // Files-tab inline preview path. Controlled when ChatPage lifts it (via
  // `previewPath`/`onPreviewPathChange`) — that keeps it alive across panel
  // collapse and lets a chat-link open of the same file route back to THIS
  // editor instead of a competing document tab (one editor per path). Falls
  // back to internal state when unmanaged. `null` = show the file list.
  const [localPreview, setLocalPreview] = useState<string | null>(null)
  const controlledPreview = onPreviewPathChange !== undefined
  const previewPathValue = controlledPreview ? (previewPath ?? null) : localPreview
  const setPreviewPath = useCallback((p: string | null) => {
    if (controlledPreview) onPreviewPathChange?.(p); else setLocalPreview(p)
  }, [controlledPreview, onPreviewPathChange])
  // The panel-level Escape-to-collapse handler (below) must defer to the inline
  // editor when a file is open: Escape then returns to the list via
  // MarkdownPanel's own guarded close (which prompts on unsaved edits) instead
  // of collapsing the whole panel out from under the editor. A ref avoids
  // re-registering the listener on every open/close.
  const previewOpenRef = useRef(false)
  previewOpenRef.current = previewPathValue != null
  const reduxTab = useAppSelector(s => s.chat.activityTab)
  const [tab, setTab] = useState<'changes' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'>(reduxTab === ('nav' as string) ? 'files' : reduxTab)
  const hasSources = (sources?.length || 0) > 0
  const explicitTab = useRef(false)
  const containerRef = useRef<HTMLDivElement>(null)
  // Exception-first ordering: agents needing attention (failed, stalled,
  // retrying, pending approval) sort to the top; the healthy/finished
  // majority follows. Stable within a rank (insertion order preserved).
  const ids = useMemo(() => {
    const rank = (a: SubagentActivity | undefined) => {
      if (!a) return 9
      if (a.status === 'error') return 0
      if (a.retrying) return 1
      if (a.stalled) return 2
      if (a.status === 'pending') return 3
      if (a.status === 'running' || a.status === 'tool') return 4
      if (a.status === 'stopped') return 5
      return 6 // done
    }
    return Object.keys(subagents).sort((x, y) => rank(subagents[x]) - rank(subagents[y]))
  }, [subagents])
  const hasSubagents = ids.length > 0
  // Render cap: bounds DOM at 60-100 agents; exceptions are always within
  // the cap thanks to the ordering above.
  const [showAllSubagents, setShowAllSubagents] = useState(false)
  const visibleIds = showAllSubagents ? ids : ids.slice(0, 30)
  const cappedCount = ids.length - visibleIds.length
  // 1-click transcript: a chip row click selects an agent — ensure it is
  // rendered (even past the cap), scrolled to, expanded, and disk-loaded.
  const selectedSubagentId = useAppSelector(s => s.chat.selectedSubagentId)
  const dispatchRedux = useAppDispatch()
  useEffect(() => {
    if (selectedSubagentId && !visibleIds.includes(selectedSubagentId) && ids.includes(selectedSubagentId)) {
      setShowAllSubagents(true)
    }
  }, [selectedSubagentId, visibleIds, ids])
  const terminalIds = useMemo(
    () => ids.filter(id => ['done', 'error', 'stopped'].includes(subagents[id]?.status ?? '')),
    [ids, subagents],
  )
  const failedRetryableIds = useMemo(
    () => ids.filter(id => subagents[id]?.status === 'error' && !id.startsWith('native:')),
    [ids, subagents],
  )
  const [retryingFailed, setRetryingFailed] = useState(false)
  const retryFailed = useCallback(() => {
    setRetryingFailed(true)
    Promise.allSettled(failedRetryableIds.map(id => api.spawnRetry(id))).finally(() => setRetryingFailed(false))
  }, [failedRetryableIds])
  const dismissDone = useCallback(() => {
    // Slot-scoped by construction: delete exactly this slot's terminal cards
    // by id — the global DELETE /api/spawn clear would nuke other sessions'
    // completed agents too (their cards would 404 on status/output).
    for (const id of terminalIds) api.spawnDelete(id).catch(() => {})
    dispatchRedux(clearTerminalSubagents({ slot }))
  }, [dispatchRedux, slot, terminalIds])

  // Dynamic Workflow runs (M6) — dedup + caching + self-managed polling
  const { data: wfRuns = [] } = useQuery<WfRunRow[]>({
    queryKey: ['workflow-runs'],
    queryFn: () =>
      fetch('/api/workflows/runs', { credentials: 'same-origin' })
        .then(r => (r.ok ? r.json() : { runs: [] }))
        .then(d => (Array.isArray(d?.runs) ? d.runs : [])),
    enabled: open,
    refetchInterval: 2500,
  })
  const wfRunsForSlot = wfRuns.filter(r => runBelongsToSlot(r.session_key, slot))
  const wfRunningCount = wfRunsForSlot.filter(r => r.status === 'running').length

  const visibleLog = toolLog.filter(e => e.type !== 'reasoning')

  // Subagent events are subscribed eagerly at WS connect time — no need to toggle here.

  useEffect(() => { setTab(reduxTab === ('nav' as string) ? 'files' : reduxTab); explicitTab.current = true }, [reduxTab])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // When an inline file is open, let MarkdownPanel's own Escape/close guard
      // handle it (return to the list, prompting on unsaved edits) rather than
      // collapsing the panel and unmounting the editor mid-edit.
      if (previewOpenRef.current) return
      e.preventDefault(); onToggle()
    }
    const el = containerRef.current
    el?.addEventListener('keydown', handler)
    return () => el?.removeEventListener('keydown', handler)
  }, [open, onToggle])

  // Auto-switch to subagents tab when subagents or spawn approvals first appear
  const hadSubagents = useRef(false)
  const hasSpawnApprovals = visibleLog.some(e => e.type === 'approval' && isSpawnApproval(e))
  const hasSubagentActivity = hasSubagents || hasSpawnApprovals
  useEffect(() => {
    if (hasSubagentActivity && !hadSubagents.current && !explicitTab.current) setTab('subagents')
    hadSubagents.current = hasSubagentActivity
    explicitTab.current = false
  }, [hasSubagentActivity])

  if (!open) return null

  // When a `view` prop is supplied, SidePanel owns the tab strip — render only
  // that view and skip the internal SegmentedControl.
  const requestedTab = view ?? tab
  const effectiveTab = requestedTab === 'changes' && !hasSources ? 'files' : requestedTab

  const TABS: { key: typeof tab; label: string; icon: ReactNode; count?: number }[] = [
    ...(hasSources ? [{ key: 'changes' as const, label: 'Changes', icon: <GitPullRequest size={13} />, count: sources!.length }] : []),
    { key: 'files', label: 'Files', icon: <FileText size={13} />, count: files?.length || 0 },
    { key: 'artifacts', label: 'Artifacts', icon: <Component size={13} /> },
    { key: 'subagents', label: 'Subagents', icon: <Bot size={13} />, count: ids.length + visibleLog.filter(isSpawnApproval).length },
    { key: 'workflows', label: 'Workflows', icon: <Workflow size={13} />, count: wfRunningCount },
    { key: 'logs', label: 'Logs', icon: <ScrollText size={13} /> },
    { key: 'side', label: 'Side', icon: <MessageSquare size={13} /> },
  ]

  return (
    // Focusable container so the imperative Escape keydown listener (attached to
    // containerRef in the effect above) has a focus target; the panel itself is
    // a region, not an interactive control.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div ref={containerRef} role="region" aria-label="Activity" className="flex flex-col h-full bg-bg relative" tabIndex={0}>
      {/* Tab bar — hidden when SidePanel drives the view via the `view` prop. */}
      {!view && (
        <div className="px-3 py-2 shrink-0 flex justify-center">
          <SegmentedControl
            segments={TABS}
            value={effectiveTab}
            onChange={t => { setTab(t); explicitTab.current = true; dispatch(openActivityToTab(t)) }}
            layoutId="activity-tab"
          />
        </div>
      )}

      {/* Changes (pull request sources) view */}
      {effectiveTab === 'changes' && hasSources && (
        <div className="flex-1 min-h-0 overflow-hidden">
          <PullRequestPanel
            sources={sources!}
            selectedUrl={selectedSourceUrl || ''}
            onSelect={onSelectSource || (() => {})}
            onAddToChat={onAddToChat || (() => {})}
          />
        </div>
      )}

      {/* Subagents tab */}
      {effectiveTab === 'subagents' && (
        <div className="flex-1 overflow-y-auto py-2">
          {/* Batch controls (scale): retry failures, clear the finished pile */}
          {(failedRetryableIds.length > 0 || terminalIds.length > 0) && (
            <div className="mx-2 mb-2 flex flex-wrap items-center gap-1.5">
              {failedRetryableIds.length > 0 && (
                <button
                  className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-accent/40 text-accent/80 hover:bg-accent/10 hover:text-accent cursor-pointer transition-all bg-transparent disabled:opacity-50 shrink-0 whitespace-nowrap"
                  onClick={retryFailed}
                  disabled={retryingFailed}
                  data-testid="retry-failed-btn"
                >
                  <RotateCcw size={11} className={retryingFailed ? 'animate-spin' : ''} /> Retry failed ({failedRetryableIds.length})
                </button>
              )}
              {terminalIds.length > 0 && (
                <button
                  className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all bg-transparent shrink-0 whitespace-nowrap"
                  onClick={dismissDone}
                  data-testid="dismiss-done-btn"
                >
                  <X size={11} /> Dismiss done ({terminalIds.length})
                </button>
              )}
            </div>
          )}
          {/* Pending approvals */}
          {visibleLog.filter(isSpawnApproval).map((entry, i) => (
            <ApprovalEntry key={`a${i}`} entry={entry} slot={slot} />
          ))}
          {hasSubagents ? (
            <>
              {visibleIds.map((id, i) => (
                <SubagentPane
                  key={id}
                  a={subagents[id]}
                  onClick={() => setSelected(i)}
                  selected={id === selectedSubagentId}
                />
              ))}
              {cappedCount > 0 && (
                <button
                  className="mx-2 mb-3 w-[calc(100%-16px)] text-[12px] text-muted hover:text-text py-2 rounded border border-dashed border-border cursor-pointer bg-transparent transition-colors"
                  onClick={() => setShowAllSubagents(true)}
                  data-testid="show-all-subagents"
                >
                  Show all ({ids.length})
                </button>
              )}
            </>
          ) : visibleLog.filter(isSpawnApproval).length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-muted/30 gap-2">
              <span className="text-[24px]"><Bot className="lucide-inline" /></span>
              <span className="text-[13px]">No subagents running</span>
            </div>
          )}
        </div>
      )}

      {/* Workflows tab (M6): live dynamic-workflow runs */}
      {effectiveTab === 'workflows' && (
        <div className="flex-1 overflow-y-auto py-2 px-3 flex flex-col gap-2">
          {wfRunsForSlot.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted/30 gap-2">
              <span className="text-[24px]"><Workflow className="lucide-inline" /></span>
              <span className="text-[13px]">No workflow runs</span>
              <span className="text-[11px] text-center px-4">
                Ask me to &quot;use a dynamic workflow to …&quot; — runs from this chat appear here live.
              </span>
            </div>
          ) : (
            wfRunsForSlot.map(r => <WorkflowSidebarRow key={r.run_id} row={r} />)
          )}
        </div>
      )}

      {/* Logs tab — LogViewer is an edge-to-edge page component; give it a
          little breathing room inside the panel. */}
      {effectiveTab === 'logs' && (
        <div className="flex-1 min-h-0 flex flex-col px-2 pb-2 pt-1">
          <LogViewer compact />
        </div>
      )}

      {/* Files tab */}
      {effectiveTab === 'files' && (() => {
        // Inline file preview: opening a file from this tab keeps it HERE
        // (no new document tab) — the list is swapped for the file's content
        // with a "Back to files" bar. A thin host of the shared MarkdownPanel
        // editor (keyed by path); falls back to the tab opener only if no save
        // handler was wired (host without editing).
        if (previewPathValue && onFileSave) {
          return (
            <FilePreview
              key={previewPathValue}
              path={previewPathValue}
              slot={slot}
              onBack={() => setPreviewPath(null)}
              onFileSave={onFileSave}
              onSubmitComments={onSubmitComments}
            />
          )
        }
        // One editor per path: if this file is already open as a document tab,
        // focus that tab instead of spawning a second (inline) editor for it.
        const openInline = onFileSave
          ? (p: string) => { if (openDocPaths?.has(p)) onFileOpen?.(p); else setPreviewPath(p) }
          : onFileOpen
        const changed = (files || []).filter(f => f.source === 'tool')
        // Hide links that are already surfaced in the Changes tab (its `sources`);
        // keep every other link — including cr-classified hosts (Bitbucket,
        // self-hosted, code reviews) that the Changes parser can't render, so
        // they stay reachable in Resources instead of vanishing from the panel.
        const sourceUrls = new Set((sources || []).map(s => resourceKey(s.url)))
        const resourceLinks = dedupResourceLinks((navLinks || []).filter(l => !sourceUrls.has(resourceKey(l.url))))
        return (
          <div className="flex-1 flex flex-col overflow-hidden">
            <div className="flex-1 overflow-y-auto py-2">
              {(changed.length === 0 && resourceLinks.length === 0) ? (
                <div className="flex-1 flex items-center justify-center text-muted text-[13px] py-8">No files changed yet</div>
              ) : (
                <>
                  {changed.length > 0 && (
                    <div className="px-3 mb-4">
                      <div className="flex items-center gap-2 my-2">
                        <span className="text-[14px] font-semibold text-muted">Changed files</span>
                        <span className="flex-1 h-px bg-border" />
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {changed.map(f => <FileTile key={f.path} f={f} onFileOpen={openInline} onFileRemove={onFileRemove} />)}
                      </div>
                    </div>
                  )}
                  {resourceLinks.length > 0 && (
                    <div className="px-3 mb-4">
                      <div className="flex items-center gap-2 my-2">
                        <span className="text-[14px] font-semibold text-muted">Resources</span>
                        <span className="flex-1 h-px bg-border" />
                        {navResolving && <span className="text-[10px] text-accent animate-pulse">resolving...</span>}
                      </div>
                      <div className="flex flex-col gap-0.5">
                        {resourceLinks.map((link, i) => (
                          <a
                            key={i}
                            href={link.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-center gap-2 px-2 py-1 rounded hover:bg-bg-hover transition-colors no-underline group"
                          >
                            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${RESOURCE_TYPE_COLORS[link.type] || RESOURCE_TYPE_COLORS.other}`}>
                              {RESOURCE_TYPE_LABELS[link.type] || 'Link'}
                            </span>
                            <span className="text-[12px] text-text truncate group-hover:text-accent transition-colors">
                              {link.label}
                            </span>
                          </a>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )
      })()}

      {/* Artifacts tab (in-session documents) */}
      {effectiveTab === 'artifacts' && <SessionArtifactsTab slot={slot} onFileOpen={onFileOpen} />}

      {/* Side tab */}
      {effectiveTab === 'side' && <SideChat slot={slot} />}

      {/* Scroll to bottom button (tools tab only) */}
    </div>
  )
}
