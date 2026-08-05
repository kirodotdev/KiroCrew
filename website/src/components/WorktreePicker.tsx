import { useState, useEffect, useCallback, useRef, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { GitBranch, Trash2, Plus, Loader2, Check, AlertTriangle } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

/**
 * WorktreePicker — pick, create, or remove a git worktree for the active
 * chat session's project repo (issue #1607).
 *
 * Consumes three endpoints: `GET /api/worktree/list`, `POST /api/worktree/create`,
 * and `POST /api/worktree/remove`. Selecting a worktree hands its path back to
 * the caller via `onSelect`, which retargets the session's project directory
 * (the same `chatSlotProject` path the ProjectPicker uses). A worktree already
 * active in ANOTHER session is shown but not selectable, and removing one is
 * refused server-side unless it is clean (or the user confirms a force removal).
 */

export interface WorktreeRow {
  path: string
  branch: string
  head: string
  is_main: boolean
  detached: boolean
  bare: boolean
  locked: boolean
  dirty: boolean | null
  active_session: string | null
}

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The session's project directory (a path inside the repo). Picker is inert without it. */
  repo?: string
  /** The active chat slot key, so a worktree bound to THIS session reads as "current". */
  activeSlot?: string | null
  anchorRef?: RefObject<HTMLElement | null>
  anchorRect?: DOMRect | null
  /** Switch the session to `path`. Called after selecting or creating a worktree. */
  onSelect: (path: string) => void
}

function tail(p: string): string {
  return p.split('/').filter(Boolean).pop() || p
}

export default function WorktreePicker({ open, onOpenChange, repo, activeSlot, anchorRef, anchorRect, onSelect }: Props) {
  const [rows, setRows] = useState<WorktreeRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [branch, setBranch] = useState('')
  const [creating, setCreating] = useState(false)
  // Path pending a force-remove confirmation (dirty worktree), and busy path.
  const [confirmForce, setConfirmForce] = useState('')
  const [busyPath, setBusyPath] = useState('')
  const dropRef = useRef<HTMLDivElement>(null)
  const anchorRectRef = useRef<DOMRect | null>(anchorRect ?? null)
  anchorRectRef.current = anchorRect ?? null

  const getAnchorRect = useCallback((): DOMRect | null => {
    if (anchorRef?.current && typeof anchorRef.current.getBoundingClientRect === 'function') {
      return anchorRef.current.getBoundingClientRect()
    }
    return anchorRectRef.current
  }, [anchorRef])

  const load = useCallback(() => {
    if (!repo) return
    setLoading(true)
    setError('')
    api.worktreeList(repo)
      .then(d => { setRows(d.worktrees || []) })
      .catch(e => { setError((e && e.message) || i18nT('components.worktreePicker.failed_to_list_worktrees')) })
      .finally(() => setLoading(false))
  }, [repo])

  useEffect(() => {
    if (!open) return
    setBranch(''); setConfirmForce(''); setError('')
    load()
  }, [open, load])

  // Outside-click / Escape to dismiss, mirroring ProjectPicker.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onOpenChange(false) }
    let cleanup = () => {}
    const timer = setTimeout(() => {
      const handler = (e: MouseEvent) => {
        if (dropRef.current && dropRef.current.contains(e.target as Node)) return
        const r = getAnchorRect()
        if (r && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
        onOpenChange(false)
      }
      document.addEventListener('mousedown', handler)
      cleanup = () => document.removeEventListener('mousedown', handler)
    }, 0)
    document.addEventListener('keydown', onKey)
    return () => { clearTimeout(timer); cleanup(); document.removeEventListener('keydown', onKey) }
  }, [open, onOpenChange, getAnchorRect])

  const selectRow = (w: WorktreeRow) => {
    // A worktree owned by another session is not selectable — two sessions
    // sharing a working tree is exactly the mixing this feature prevents.
    if (w.active_session && w.active_session !== activeSlot) return
    onSelect(w.path)
    onOpenChange(false)
  }

  const create = () => {
    const b = branch.trim()
    if (!repo || !b || creating) return
    setCreating(true)
    setError('')
    api.createWorktree(repo, b)
      .then(res => {
        if (res?.path) { onSelect(res.path); onOpenChange(false) }
        else setError(res?.error || i18nT('components.worktreePicker.worktree_creation_failed'))
      })
      .catch(e => setError((e && e.message) || i18nT('components.worktreePicker.worktree_creation_failed')))
      .finally(() => setCreating(false))
  }

  const remove = (w: WorktreeRow, force: boolean) => {
    if (!repo || busyPath) return
    setBusyPath(w.path)
    setError('')
    api.worktreeRemove(repo, w.path, force)
      .then(res => {
        if (res?.ok) { setConfirmForce(''); load() }
        else if (res?.dirty) setConfirmForce(w.path)     // needs an explicit force confirmation
        else setError(res?.error || i18nT('components.worktreePicker.worktree_removal_failed'))
      })
      .catch(e => {
        // A 409 for a dirty tree arrives as an ApiError; surface the confirm row.
        const msg = (e && e.message) || ''
        if (/uncommitted/i.test(msg)) setConfirmForce(w.path)
        else setError(msg || i18nT('components.worktreePicker.worktree_removal_failed'))
      })
      .finally(() => setBusyPath(''))
  }

  const anchorR = getAnchorRect()
  if (!open || !anchorR) return null

  return createPortal(
    <div
      ref={dropRef}
      role="dialog"
      aria-label={i18nT('components.worktreePicker.worktrees')}
      className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl w-[440px] flex flex-col overflow-hidden animate-slide-up"
      style={(() => {
        const minH = 200
        const spaceBelow = window.innerHeight - anchorR.bottom - 8
        const flipUp = spaceBelow < minH || anchorR.bottom > window.innerHeight / 2
        const left = Math.max(8, Math.min(anchorR.right - 440, window.innerWidth - 448))
        if (flipUp) {
          const spaceAbove = anchorR.top - 8
          return { bottom: window.innerHeight - anchorR.top + 4, left, height: Math.min(480, Math.max(200, spaceAbove)) }
        }
        return { top: anchorR.bottom + 4, left, height: Math.min(480, Math.max(200, spaceBelow)) }
      })()}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <GitBranch size={13} className="text-accent shrink-0" />
        <span className="text-[12px] font-semibold text-text">{i18nT('components.worktreePicker.worktrees')}</span>
        {loading && <Loader2 size={12} className="animate-spin text-muted ml-auto" />}
      </div>

      {error && (
        <div className="px-3 py-2 text-[12px] flex items-start gap-1.5" style={{ color: 'var(--danger)' }}>
          <AlertTriangle size={12} className="shrink-0 mt-0.5" /> <span className="min-w-0 break-words">{error}</span>
        </div>
      )}

      <div role="listbox" aria-label={i18nT('components.worktreePicker.worktrees')} className="overflow-y-auto flex-1 min-h-0">
        {!loading && rows.length === 0 && !error && (
          <div className="px-3 py-6 text-[12px] text-muted text-center">{i18nT('components.worktreePicker.no_worktrees')}</div>
        )}
        {rows.map(w => {
          const ownedElsewhere = !!w.active_session && w.active_session !== activeSlot
          const isCurrent = !!w.active_session && w.active_session === activeSlot
          const label = w.detached ? i18nT('components.worktreePicker.detached') : (w.branch || tail(w.path))
          return (
            <div key={w.path} className={`px-3 py-2 flex items-center gap-2 border-b border-border/50 ${ownedElsewhere ? 'opacity-60' : ''}`}>
              <button
                role="option"
                aria-selected={isCurrent}
                disabled={ownedElsewhere}
                onClick={() => selectRow(w)}
                title={ownedElsewhere ? i18nT('components.worktreePicker.in_use_by_another_session') : w.path}
                className="flex-1 min-w-0 text-left flex flex-col gap-0.5 cursor-pointer disabled:cursor-not-allowed bg-transparent border-none p-0"
              >
                <div className="flex items-center gap-1.5 min-w-0">
                  {isCurrent && <Check size={12} className="text-accent shrink-0" />}
                  <span className="text-[13px] font-mono font-semibold text-text truncate">{label}</span>
                  {w.is_main && <span className="text-[10px] px-1 rounded bg-accent-subtle text-accent shrink-0">{i18nT('components.worktreePicker.main')}</span>}
                  {isCurrent && <span className="text-[10px] px-1 rounded bg-accent-subtle text-accent shrink-0">{i18nT('components.worktreePicker.this_session')}</span>}
                  {ownedElsewhere && <span className="text-[10px] px-1 rounded shrink-0" style={{ background: 'var(--warn-subtle)', color: 'var(--warn)' }}>{i18nT('components.worktreePicker.in_use')}</span>}
                  {w.dirty && <span className="text-[10px] px-1 rounded shrink-0" style={{ background: 'var(--warn-subtle)', color: 'var(--warn)' }}>{i18nT('components.worktreePicker.uncommitted')}</span>}
                </div>
                <span className="text-[11px] text-muted truncate">{w.path}</span>
              </button>
              {!w.is_main && (
                confirmForce === w.path ? (
                  <div className="flex items-center gap-1 shrink-0">
                    <span className="text-[10px] text-muted">{i18nT('components.worktreePicker.remove_anyway')}</span>
                    <button
                      onClick={() => remove(w, true)}
                      disabled={busyPath === w.path}
                      className="text-[10px] px-1.5 py-0.5 rounded"
                      style={{ background: 'var(--danger-subtle)', color: 'var(--danger)' }}
                    >{i18nT('components.worktreePicker.force')}</button>
                    <button onClick={() => setConfirmForce('')} className="text-[10px] px-1.5 py-0.5 rounded text-muted hover:text-text">{i18nT('components.worktreePicker.cancel')}</button>
                  </div>
                ) : (
                  <button
                    onClick={() => remove(w, false)}
                    disabled={busyPath === w.path || (!!w.active_session)}
                    title={w.active_session ? i18nT('components.worktreePicker.in_use_by_another_session') : i18nT('components.worktreePicker.remove_worktree')}
                    aria-label={i18nT('components.worktreePicker.remove_worktree')}
                    className="p-1 rounded text-muted hover:text-[var(--danger)] hover:bg-bg-hover shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {busyPath === w.path ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                  </button>
                )
              )}
            </div>
          )
        })}
      </div>

      {/* Create a new worktree from a branch name. */}
      <div className="p-2 border-t border-border flex items-center gap-1.5">
        <GitBranch size={13} className="text-muted shrink-0" />
        <input
          type="text"
          value={branch}
          onChange={e => setBranch(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); create() } }}
          placeholder={i18nT('components.worktreePicker.new_branch_placeholder')}
          aria-label={i18nT('components.worktreePicker.new_branch_name')}
          className="flex-1 min-w-0 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus:border-accent"
        />
        <button
          onClick={create}
          disabled={!branch.trim() || creating || !repo}
          className="inline-flex items-center gap-1 text-[12px] px-2.5 py-1.5 rounded-md bg-accent/20 text-accent hover:bg-accent/30 disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
        >
          {creating ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
          {i18nT('components.worktreePicker.create')}
        </button>
      </div>
    </div>,
    document.body,
  )
}
