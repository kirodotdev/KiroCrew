import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, ArrowLeft, GitBranch, Loader2, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { api } from '../api/client'
import { fmtBytes } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { WorktreeEntry, WorktreeListResponse, WorktreeVerdict } from '../types'

/**
 * Worktrees of one repository, as a popover anchored to the composer shelf.
 *
 * Anchored rather than centered: it belongs to the shelf control that opened it,
 * the same as the project/model/agent pickers on that row. A centered overlay
 * reads as a mode switch, which is the wrong weight for glancing at a list.
 *
 * The delete confirmation stays INSIDE this popover for the same reason — a
 * second floating layer over the first is where "just checking my trees" starts
 * to feel like surgery.
 *
 * `dirty` (a non-empty `git status --porcelain`, untracked files included) means
 * work exists ONLY in that directory. The server refuses such a tree with 409
 * unless `force` is passed, and `force` is only ever sent from the confirm step.
 */

/**
 * Verdict chip text, resolved per render rather than held in a module constant:
 * a constant would capture whichever locale happened to be active at import and
 * never follow a language switch.
 *
 * Keys are full literals so extractors and dead-key tooling can see them — see
 * `UPDATE_ERROR_KEYS` in AboutPanel.tsx for the canonical pattern.
 */
const VERDICT_LABEL_KEY: Record<WorktreeVerdict, string> = {
  merged: 'components.worktreePanel.verdict_merged',
  merged_dirty: 'components.worktreePanel.verdict_merged_dirty',
  empty: 'components.worktreePanel.verdict_empty',
  fresh: 'components.worktreePanel.verdict_fresh',
  active: 'components.worktreePanel.verdict_active',
  dirty_check_failed: 'components.worktreePanel.verdict_unknown',
  base_unknown: 'components.worktreePanel.verdict_no_base',
} as const

function verdictLabel(verdict: WorktreeVerdict): string {
  const key = VERDICT_LABEL_KEY[verdict]
  return key ? i18nT(key) : verdict
}

/** `null` is "we could not tell", which must never read as "clean". */
function dirtyLabel(dirty: boolean | null): string {
  if (dirty === null) return i18nT('components.worktreePanel.state_unknown')
  return dirty
    ? i18nT('components.worktreePanel.state_dirty')
    : i18nT('components.worktreePanel.state_clean')
}

/** Recyclable reads positive; a refusal that exists to protect work reads as a warning. */
function verdictTone(entry: WorktreeEntry): string {
  if (entry.recyclable) return 'border-ok/40 text-ok bg-ok/10'
  if (entry.verdict === 'merged_dirty' || entry.verdict === 'dirty_check_failed') {
    return 'border-warn/40 text-warn bg-warn/10'
  }
  return 'border-border text-muted'
}

function shortName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

const WIDTH = 460

/**
 * Name a worktree the way Claude Code does when you omit one — an adjective,
 * a participle and an animal, e.g. `bright-running-fox`. A generated name is
 * what makes "give me a tree for this" a single click instead of a naming
 * decision, and it also marks the tree as tooling-made: an unnamed tree that
 * ends up clean is safe to reclaim without asking.
 */
const NAME_ADJECTIVES = ['bright', 'quiet', 'brave', 'amber', 'swift', 'calm', 'keen', 'warm']
const NAME_PARTICIPLES = ['running', 'drifting', 'humming', 'climbing', 'roaming', 'gliding']
const NAME_ANIMALS = ['fox', 'otter', 'heron', 'lynx', 'marten', 'ibis', 'shrike', 'tapir']

function generatedBranch(): string {
  const pick = (xs: string[]) => xs[Math.floor(Math.random() * xs.length)]
  return `wt/${pick(NAME_ADJECTIVES)}-${pick(NAME_PARTICIPLES)}-${pick(NAME_ANIMALS)}`
}

export interface WorktreePanelProps {
  /** Main checkout the worktrees belong to. */
  repo: string
  /** Worktree path of the session that opened the popover, marked in the list. */
  activePath?: string
  /** Bounding box of the shelf button, so the popover sits over it. */
  anchorRect?: DOMRect | null
  /**
   * Create a worktree on ``branch`` and open a session scoped to it. Provided by
   * the page, which owns session creation; the popover only collects the name.
   */
  onCreate?: (branch: string) => Promise<void>
  /**
   * Move THIS session into ``entry`` — Claude Code's `EnterWorktree`. The tree
   * the session leaves stays on disk untouched.
   */
  onEnter?: (entry: WorktreeEntry) => Promise<void>
  onClose: () => void
}

export function WorktreePanel({
  repo, activePath, anchorRect, onCreate, onEnter, onClose,
}: WorktreePanelProps) {
  const [data, setData] = useState<WorktreeListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState<WorktreeEntry | null>(null)
  const [busyPath, setBusyPath] = useState('')
  const [newBranch, setNewBranch] = useState('')
  const [creating, setCreating] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  const load = useCallback(async (withSize: boolean, background = false, fresh = false) => {
    if (!background) setLoading(true)
    setError('')
    try {
      const res = await api.listWorktrees(repo, withSize, fresh)
      setData(res)
      if (res.error) setError(res.error)
    } catch (err) {
      // A failed background size pass must not blank the list it is enriching.
      if (!background) setError(err instanceof Error ? err.message : i18nT('components.worktreePanel.could_not_list_worktrees'))
    } finally {
      if (!background) setLoading(false)
    }
  }, [repo])

  // Two passes on purpose: sizing walks every file in every tree, which on a
  // large repo is seconds. Paint the git state first, then fill the sizes in
  // without a second spinner.
  useEffect(() => {
    let live = true
    void (async () => {
      await load(false)
      if (live) await load(true, true)
    })()
    return () => { live = false }
  }, [load])

  // Dismiss like the sibling shelf pickers: Escape, or a click outside. Guard the
  // outside-click on the confirm step so a stray click cannot both dismiss the
  // popover and leave the user unsure whether the removal ran.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (confirming) setConfirming(null)
      else onClose()
    }
    const onDown = (e: MouseEvent) => {
      if (busyPath) return
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [onClose, confirming, busyPath])

  // The panel portals to the end of <body>, so without this a keyboard user who
  // opened it from the shelf would have to tab across the whole document to reach
  // it. Focus the dialog itself rather than its first control: the first control
  // is a destructive trash button on some rows, and landing on it would make
  // Enter-to-dismiss delete a tree. Focus returns to the opener on close so the
  // shelf does not lose the user's place.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    rootRef.current?.focus()
    return () => opener?.focus?.()
  }, [])

  const linked = useMemo(
    () => (data?.worktrees || []).filter(w => !w.is_main),
    [data],
  )
  // The main checkout is rendered as the first row rather than filtered out: it
  // is how a session leaves a worktree (Claude Code's `ExitWorktree`), and the
  // list is the only place that move belongs.
  const mainEntry = useMemo(
    () => (data?.worktrees || []).find(w => w.is_main) || null,
    [data],
  )
  const inWorktree = !!activePath

  const [entering, setEntering] = useState('')

  const enter = useCallback(async (entry: WorktreeEntry) => {
    if (!onEnter || entry.path === activePath) return
    setEntering(entry.path)
    setError('')
    try {
      await onEnter(entry)
    } catch (err) {
      setError(err instanceof Error ? err.message : i18nT('components.worktreePanel.could_not_switch_worktree'))
    } finally {
      setEntering('')
    }
  }, [onEnter, activePath])

  const remove = useCallback(async (entry: WorktreeEntry, force: boolean) => {
    setBusyPath(entry.path)
    setError('')
    try {
      const res = await api.removeWorktree(repo, entry.path, force)
      if (res.error) {
        setError(res.error)
        return
      }
      setConfirming(null)
      await load(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : i18nT('components.worktreePanel.removal_failed'))
    } finally {
      setBusyPath('')
    }
  }, [repo, load])

  // A tree the tooling named itself and that holds nothing needs no ceremony.
  // Everything else goes through the confirm step.
  const requestRemove = useCallback((entry: WorktreeEntry) => {
    if (entry.dirty === false && entry.recyclable) {
      void remove(entry, false)
      return
    }
    setConfirming(entry)
  }, [remove])

  const create = useCallback(async () => {
    // An empty name is not an error: Claude Code generates one, which keeps
    // "give me a tree" a single click.
    const branch = newBranch.trim() || generatedBranch()
    if (!onCreate) return
    setCreating(true)
    setError('')
    try {
      await onCreate(branch)
      // The page switches to the new session, which unmounts this popover; only a
      // failure path gets this far in a state worth resetting.
      setNewBranch('')
    } catch (err) {
      setError(err instanceof Error ? err.message : i18nT('components.worktreePanel.could_not_create_the_worktree'))
    } finally {
      setCreating(false)
    }
  }, [newBranch, onCreate])

  const position = (() => {
    const left = anchorRect
      ? Math.max(8, Math.min(anchorRect.left, window.innerWidth - WIDTH - 8))
      : Math.max(8, window.innerWidth / 2 - WIDTH / 2)
    if (!anchorRect) return { bottom: 80, left }
    const spaceBelow = window.innerHeight - anchorRect.bottom - 8
    if (spaceBelow < 220 || anchorRect.bottom > window.innerHeight / 2) {
      return { bottom: window.innerHeight - anchorRect.top + 6, left }
    }
    return { top: anchorRect.bottom + 6, left }
  })()

  return createPortal(
    <div
      ref={rootRef}
      role="dialog"
      tabIndex={-1}
      aria-label={i18nT('components.worktreePanel.worktrees')}
      className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl flex flex-col overflow-hidden animate-slide-up outline-none"
      style={{ ...position, width: WIDTH, maxHeight: '52vh' }}
    >
      {confirming ? (
        <ConfirmRemove
          entry={confirming}
          busy={busyPath === confirming.path}
          error={error}
          onBack={() => { setConfirming(null); setError('') }}
          onConfirm={() => void remove(confirming, true)}
        />
      ) : (
        <>
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
            <GitBranch size={13} className="text-accent shrink-0" />
            <span className="text-[12px] font-medium shrink-0">{i18nT('components.worktreePanel.worktrees')}</span>
            <span className="text-[10.5px] text-muted font-mono truncate" title={repo}>
              {shortName(repo)}
            </span>
            <div className="ml-auto flex items-center gap-2 text-[10.5px] text-muted font-mono shrink-0">
              <span>{!data && loading
                ? '…'
                : i18nT('components.worktreePanel.n_trees', { count: linked.length })
                  + (data?.disk_bytes ? ` · ${fmtBytes(data.disk_bytes)}` : '')}</span>
              <button
                className="p-1 rounded hover:bg-bg-hover disabled:opacity-40"
                onClick={() => void load(true, false, true)}
                disabled={loading}
                aria-label={i18nT('components.worktreePanel.refresh')}
              >
                {loading ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
              </button>
              <button className="p-1 rounded hover:bg-bg-hover" onClick={onClose} aria-label={i18nT('components.worktreePanel.close')}>
                <X size={12} />
              </button>
            </div>
          </div>

          {error && (
            <div className="px-3 py-1.5 text-[11px] text-warn bg-warn/10 border-b border-border">
              {error}
            </div>
          )}

          {onCreate && (
            /* Creating a tree lives next to seeing them: the answer to "what
               trees exist" and "give me one more" belong in the same place. */
            <div className="flex items-center gap-1.5 px-3 py-2 border-b border-border bg-bg/40">
              <Plus size={12} className="text-muted shrink-0" />
              <input
                value={newBranch}
                onChange={e => setNewBranch(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') void create() }}
                placeholder={i18nT('components.worktreePanel.new_branch_leave_empty_for_a_generated_name')}
                className="flex-1 min-w-0 bg-transparent border-none outline-none text-[11.5px] font-mono placeholder:text-muted/70"
                aria-label={i18nT('components.worktreePanel.new_worktree_branch')}
                disabled={creating}
              />
              <button
                className="shrink-0 px-2 py-0.5 rounded border border-border text-[11px] text-muted hover:text-accent hover:border-accent disabled:opacity-40"
                onClick={() => void create()}
                disabled={creating}
                aria-label={i18nT('components.worktreePanel.create_worktree')}
              >
                {creating ? <Loader2 size={11} className="animate-spin" /> : i18nT('components.worktreePanel.create')}
              </button>
            </div>
          )}

          <div className="overflow-y-auto">
            {loading && !data && (
              <div className="px-3 py-8 text-center text-[11.5px] text-muted flex items-center justify-center gap-2">
                <Loader2 size={12} className="animate-spin" /> {i18nT('components.worktreePanel.reading_git_state')}
              </div>
            )}
            {!loading && linked.length === 0 && (
              <div className="px-3 py-8 text-center text-[11.5px] text-muted">
                {i18nT('components.worktreePanel.no_worktrees_for_this_repository_yet')}
              </div>
            )}
            {mainEntry && onEnter && (
              /* Leaving a worktree is the same gesture as entering one, so the
                 main checkout is a row in the same list rather than a separate
                 control. Disabled while the session is already here. */
              <button
                className={`w-full text-left flex items-center gap-2 px-3 py-2 border-b border-border/40 ${inWorktree ? 'hover:bg-bg-hover cursor-pointer' : 'bg-accent/5 cursor-default'}`}
                onClick={() => void enter(mainEntry)}
                disabled={!inWorktree || !!entering}
                aria-label={i18nT('components.worktreePanel.enter_main_checkout')}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-[12px] truncate">
                      {mainEntry.branch || i18nT('components.worktreePanel.main_checkout')}
                    </span>
                    <span className="text-[10px] text-muted shrink-0">{i18nT('components.worktreePanel.main_checkout')}</span>
                    {!inWorktree && (
                      <span className="text-[10px] text-accent shrink-0">{i18nT('components.worktreePanel.this_session')}</span>
                    )}
                  </div>
                  <div className="text-[10.5px] text-muted font-mono truncate">
                    {dirtyLabel(mainEntry.dirty)}
                    {' · '}{shortName(repo)}
                  </div>
                </div>
                {entering === mainEntry.path
                  ? <Loader2 size={12} className="animate-spin shrink-0 text-muted" />
                  : inWorktree && <ArrowLeft size={12} className="shrink-0 text-muted rotate-180" />}
              </button>
            )}
            {linked.map(entry => (
              <div
                key={entry.path}
                className={`group flex items-center gap-2 border-b border-border/40 last:border-b-0 ${entry.path === activePath ? 'bg-accent/5' : 'hover:bg-bg-hover'}`}
              >
                <button
                  className="min-w-0 flex-1 text-left px-3 py-2 bg-transparent border-none cursor-pointer disabled:cursor-default"
                  onClick={() => void enter(entry)}
                  disabled={!onEnter || entry.path === activePath || !!entering}
                  aria-label={i18nT('components.worktreePanel.enter_tree', { name: shortName(entry.path) })}
                  title={entry.path === activePath
                    ? i18nT('components.worktreePanel.this_session_is_already_here')
                    : i18nT('components.worktreePanel.move_this_session_into', { path: entry.path })}
                >
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-[12px] truncate" title={entry.branch}>
                      {entry.branch || shortName(entry.path)}
                    </span>
                    {entry.path === activePath && (
                      <span className="text-[10px] text-accent shrink-0">{i18nT('components.worktreePanel.this_session')}</span>
                    )}
                    {entering === entry.path && (
                      <Loader2 size={10} className="animate-spin shrink-0 text-muted" />
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 text-[10.5px] text-muted font-mono truncate">
                    <span className={entry.dirty === null || entry.dirty ? 'text-warn' : ''}>
                      {dirtyLabel(entry.dirty)}
                    </span>
                    <span>·</span>
                    <span>{entry.ahead > 0 ? `+${entry.ahead}` : '±0'}{entry.behind > 0 ? ` −${entry.behind}` : ''}</span>
                    {entry.size_bytes > 0 && (
                      <>
                        <span>·</span>
                        <span>{fmtBytes(entry.size_bytes)}</span>
                      </>
                    )}
                  </div>
                </button>
                <span className={`shrink-0 px-1.5 py-[1px] rounded text-[10px] border font-mono ${verdictTone(entry)}`}>
                  {verdictLabel(entry.verdict)}
                </span>
                <button
                  // Two behaviours share this control: a clean recyclable tree is
                  // removed on the first click, everything else routes through the
                  // confirm. A hover `title` cannot disclose that to a keyboard or
                  // touch user, so the tone carries it too — `ok` for the safe
                  // instant path, danger-on-hover for the one that will ask.
                  className={`shrink-0 mr-3 p-1 rounded disabled:opacity-40 ${
                    entry.recyclable && entry.dirty === false
                      ? 'text-ok hover:bg-ok/10'
                      : 'text-muted hover:text-danger hover:bg-danger/10'
                  }`}
                  onClick={() => requestRemove(entry)}
                  disabled={busyPath === entry.path || entry.path === activePath}
                  aria-label={`${i18nT('components.worktreePanel.remove_tree', { name: shortName(entry.path) })} — ${
                    entry.recyclable && entry.dirty === false
                      ? i18nT('components.worktreePanel.recyclable_removes_without_asking')
                      : i18nT('components.worktreePanel.remove_asks_first')
                  }`}
                  title={entry.path === activePath
                    ? i18nT('components.worktreePanel.leave_this_worktree_before_removing_it')
                    : entry.recyclable
                      ? i18nT('components.worktreePanel.recyclable_removes_without_asking')
                      : i18nT('components.worktreePanel.remove_asks_first')}
                >
                  {busyPath === entry.path
                    ? <Loader2 size={12} className="animate-spin" />
                    : <Trash2 size={12} />}
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>,
    document.body,
  )
}

function ConfirmRemove({
  entry, busy, error, onBack, onConfirm,
}: {
  entry: WorktreeEntry
  busy: boolean
  error: string
  onBack: () => void
  onConfirm: () => void
}) {
  const losesWork = entry.dirty !== false
  return (
    <>
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <button className="p-0.5 rounded hover:bg-bg-hover" onClick={onBack} aria-label={i18nT('components.worktreePanel.back')}>
          <ArrowLeft size={13} />
        </button>
        {losesWork && <AlertTriangle size={12} className="text-danger shrink-0" />}
        <span className="text-[12px] font-medium truncate">
          {i18nT('components.worktreePanel.remove_branch', { branch: entry.branch })}
        </span>
      </div>
      <div className="px-3 py-2.5 text-[11.5px] text-muted leading-relaxed overflow-y-auto">
        {entry.dirty === null ? (
          i18nT('components.worktreePanel.body_dirty_unknown')
        ) : entry.dirty ? (
          <><strong className="text-text">{i18nT('components.worktreePanel.uncommitted_changes')}</strong>{' '}
            {i18nT('components.worktreePanel.body_dirty')}</>
        ) : entry.ahead > 0 ? (
          i18nT('components.worktreePanel.body_clean_ahead', {
            count: entry.ahead,
            verdict: verdictLabel(entry.verdict),
          })
        ) : (
          i18nT('components.worktreePanel.body_clean_no_unlanded', {
            verdict: verdictLabel(entry.verdict),
          })
        )}
        <div className="mt-2 rounded-md border border-border bg-bg px-2 py-1.5 font-mono text-[10.5px] leading-relaxed text-text">
          <div className="truncate" title={entry.path}>{entry.path}</div>
          <div className="text-muted">
            {entry.branch} @ {entry.head} · {i18nT('components.worktreePanel.branch_ref_kept_directory_only')}
          </div>
        </div>
        {error && <div className="mt-2 text-[11px] text-warn">{error}</div>}
      </div>
      <div className="flex justify-end gap-2 px-3 py-2 border-t border-border bg-bg">
        <button className="px-2.5 py-1 rounded border border-border text-[11.5px] hover:bg-bg-hover" onClick={onBack}>
          {i18nT('components.worktreePanel.cancel')}
        </button>
        <button
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-danger/60 text-danger text-[11.5px] hover:bg-danger/10 disabled:opacity-40"
          onClick={onConfirm}
          disabled={busy}
        >
          {busy && <Loader2 size={11} className="animate-spin" />}
          {losesWork
            ? i18nT('components.worktreePanel.delete_lose_changes')
            : i18nT('components.worktreePanel.remove')}
        </button>
      </div>
    </>
  )
}

export default WorktreePanel
