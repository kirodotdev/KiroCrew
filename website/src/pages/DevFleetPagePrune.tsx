/**
 * The Prune merged flow's two dialogs: the review of the candidates and the
 * kept worktrees, and the per-worktree progress checklist.
 */
import { type ReactNode, type Dispatch, type SetStateAction } from 'react'
import { Btn, Checkbox, Badge } from '../components/ui'
import Modal from '../components/Modal'
import ErrorNotice from '../components/ErrorNotice'
import { LoaderCircle, Check, X, ShieldAlert } from 'lucide-react'
import { i18nT } from '../i18n/t'
import {
  pruneVerdictLabel, PRUNE_STATUS_META, pruneStatusLabel, type FleetData, type PruneDialog,
  type PruneProgress,
} from './devFleetPageModel'

/** The Prune merged review: removable candidates, closed-PR candidates and the kept worktrees an override may force. */
export function DevFleetPruneReview({
  fleet, pruneDialog, setPruneDialog, pruneSelected, setPruneSelected, pruneForceSelected,
  setPruneForceSelected, askConfirm, pruneExecute,
}: {
  fleet: FleetData | undefined
  pruneDialog: PruneDialog
  setPruneDialog: Dispatch<SetStateAction<PruneDialog | null>>
  pruneSelected: Set<string>
  setPruneSelected: Dispatch<SetStateAction<Set<string>>>
  pruneForceSelected: Set<string>
  setPruneForceSelected: Dispatch<SetStateAction<Set<string>>>
  askConfirm: (title: string, desc: ReactNode, opts?: { confirmLabel?: string; cancelLabel?: string; danger?: boolean; width?: number }) => Promise<boolean>
  pruneExecute: (rawNames: string[], rawForceNames?: string[], discardPaths?: Record<string, string[]>) => Promise<void>
}) {
  // Determine which kept worktrees are guarded (main or live — cannot be force-removed).
  const liveWt = fleet?.worktrees?.find((w) => w.is_live)
  const isGuarded = (name: string) => {
    const wt = fleet?.worktrees?.find((w) => w.name === name)
    return !!(wt?.is_main || wt?.is_live || wt?.is_staged || (liveWt && liveWt.name === name))
  }
  const hasForceSelected = pruneForceSelected.size > 0
  // A kept row is blocked by scratch alone when the backend classified its
  // dirt as untracked-only AND handed back the COMPLETE file list (the server
  // caps it, and it refuses a consent that does not cover the whole set).
  // Ticking such a row means "remove it, discarding exactly these files" —
  // the paths travel with the request so the server can verify the set has
  // not changed since it was shown, and it still refuses if a tracked file
  // turns out to be modified.
  const scratchPaths = (name: string): string[] | null => {
    const k = pruneDialog.kept.find((r) => r.name === name)
    if (!k || k.dirty_tracked !== false) return null
    const paths = k.dirty_untracked_paths ?? []
    if (!paths.length || paths.length !== k.dirty_untracked) return null
    return paths
  }
  const untrackedOnly = (name: string) => scratchPaths(name) !== null
  const handleRemove = async () => {
    const regularNames = pruneDialog.candidates.filter((c) => pruneSelected.has(c.name)).map((c) => c.name)
    const forceNames = Array.from(pruneForceSelected)
    const discardPaths: Record<string, string[]> = {}
    for (const n of forceNames) {
      const p = scratchPaths(n)
      if (p) discardPaths[n] = p
    }
    if (forceNames.length > 0) {
      const confirmed = await askConfirm(
        i18nT('pages.devFleetPage.force_remove_confirm_title'),
        i18nT('pages.devFleetPage.force_remove_confirm_desc', { count: forceNames.length }),
        { confirmLabel: i18nT('pages.devFleetPage.delete_anyway'), danger: true }
      )
      if (!confirmed) return
    }
    pruneExecute(regularNames, forceNames, discardPaths)
  }
  return (
    <Modal open={true} onClose={() => setPruneDialog(null)} title={i18nT('pages.devFleetPage.prune_worktrees')} maxWidth={480} footer={<><Btn onClick={() => setPruneDialog(null)}>{i18nT('pages.devFleetPage.cancel')}</Btn><Btn danger onClick={handleRemove}>{i18nT('pages.devFleetPage.remove_selected')}</Btn></>}>
      <div style={{ maxHeight: 360, overflowY: 'auto' }}>
        {pruneDialog.candidates.filter((c) => c.code !== 'closed').length > 0 && (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--muted)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>{i18nT('pages.devFleetPage.remove')}</div>
            {pruneDialog.candidates.filter((c) => c.code !== 'closed').map((c) => (
              <label key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', cursor: 'pointer' }}>
                <Checkbox checked={pruneSelected.has(c.name)} onChange={(e) => setPruneSelected((prev) => { const next = new Set(prev); if (e.target.checked) next.add(c.name); else next.delete(c.name); return next })} aria-label={i18nT('pages.devFleetPage.select', { name: c.name })} />
                <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: '1 1 auto', minWidth: 0 }}>{c.name}</span>
                <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap', flex: '0 1 auto', minWidth: 0, maxWidth: 'min(200px, 55%)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{pruneVerdictLabel(c.code)}</span>
              </label>
            ))}
          </div>
        )}
        {/* Closed-PR worktrees are a DISTINCT group with its own header and
            warning copy, never folded into the merged "Remove" list — a
            merged tree's content is on main by definition, a closed one's is
            not, so the operator must never mistake one for the other. Rows
            whose branch is ahead of main carry an extra per-row alarm. */}
        {/* Pre-existing duplicate of the merged-PR row, moved verbatim; dedupe is out of scope. */}
        {/* jscpd:ignore-start */}
        {pruneDialog.candidates.filter((c) => c.code === 'closed').length > 0 && (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--warn)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>{i18nT('pages.devFleetPage.remove_closed_pr')}</div>
            <p style={{ fontSize: 11, color: 'var(--muted)', margin: '0 0 6px' }}>{i18nT('pages.devFleetPage.closed_pr_not_on_main_hint')}</p>
            {pruneDialog.candidates.filter((c) => c.code === 'closed').map((c) => (
              <label key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', cursor: 'pointer' }}>
                <Checkbox checked={pruneSelected.has(c.name)} onChange={(e) => setPruneSelected((prev) => { const next = new Set(prev); if (e.target.checked) next.add(c.name); else next.delete(c.name); return next })} aria-label={i18nT('pages.devFleetPage.select', { name: c.name })} />
                <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: '1 1 auto', minWidth: 0 }}>{c.name}</span>
                {/* The status must never starve the NAME: this row selects a
                    worktree for permanent deletion, so the operator has to be
                    able to read which one. A nowrap status with no shrink
                    basis takes its full intrinsic width -- and a long
                    localized string in a 320px modal then collapses the
                    flexible name to an ellipsis. */}
                <span style={{ marginLeft: 'auto', fontSize: 11, color: c.unmerged_commits ? 'var(--danger)' : 'var(--muted)', whiteSpace: 'nowrap', flex: '0 1 auto', minWidth: 0, maxWidth: 'min(200px, 55%)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.unmerged_commits ? i18nT('pages.devFleetPage.closed_has_unmerged_commits') : pruneVerdictLabel(c.code)}</span>
              </label>
            ))}
          </div>
        )}
        {/* jscpd:ignore-end */}
        {pruneDialog.kept.length > 0 && (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--muted)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>{i18nT('pages.devFleetPage.kept')}</div>
            {pruneDialog.kept.some((k) => !isGuarded(k.name) && !k.dirty && k.code !== 'dirty_check_failed') && <p style={{ fontSize: 11, color: 'var(--muted)', margin: '0 0 6px' }}>{i18nT('pages.devFleetPage.kept_force_hint')}</p>}
            {pruneDialog.kept.some((k) => !isGuarded(k.name) && untrackedOnly(k.name)) && <p style={{ fontSize: 11, color: 'var(--muted)', margin: '0 0 6px' }}>{i18nT('pages.devFleetPage.kept_discard_untracked_hint')}</p>}
            {pruneDialog.kept.map((k) => {
              const guarded = isGuarded(k.name)
              // Disable the override only where the backend really refuses it:
              // an unverifiable tree (git status failed), or dirt that includes
              // a MODIFIED TRACKED file — unfinished work the override must not
              // destroy. Dirt that is only untracked scratch stays checkable,
              // because ticking it sends a discard for exactly those files;
              // blanket-disabling on `dirty` was what left such rows with no
              // way forward at all.
              const scratchOnly = untrackedOnly(k.name)
              const cannotForce = k.code === 'dirty_check_failed' || (!!k.dirty && !scratchOnly)
              const disabled = guarded || cannotForce
              const checked = pruneForceSelected.has(k.name)
              return (
                <label key={k.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.6 : 1 }}>
                  {guarded
                    ? <span style={{ width: 13, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}><ShieldAlert size={13} style={{ color: 'var(--muted)' }} /></span>
                    : <Checkbox checked={checked} disabled={cannotForce} onChange={(e) => setPruneForceSelected((prev) => { const next = new Set(prev); if (e.target.checked) next.add(k.name); else next.delete(k.name); return next })} aria-label={i18nT('pages.devFleetPage.force_remove', { name: k.name })} />
                  }
                  <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, color: checked ? 'var(--danger)' : guarded ? 'var(--muted)' : 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: '1 1 auto', minWidth: 0 }}>{k.name}</span>
                  {/* `min(200px, 55%)` rather than a flat 200px: a fixed cap
                      does not scale down, so in a 320px modal the status took
                      200px of it and the flexible name collapsed to nothing --
                      leaving a consequence with no visible subject. The px arm
                      keeps the roomier desktop reading. */}
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap', flex: '0 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 'min(200px, 55%)' }} title={scratchOnly ? (k.dirty_untracked_paths ?? []).join(', ') : pruneVerdictLabel(k.code)}>
                    {guarded && i18nT('pages.devFleetPage.protected_worktree')}
                    {/* A scratch-only row shows the FILENAMES it would
                        discard, not the verdict label. Two rows can carry the
                        same checkbox meaning very different destruction --
                        "force-delete unmerged work" vs "throw away a probe
                        script" -- and a hover title is the one disclosure a
                        keyboard or touch user never reaches. The filenames are
                        data, so they need no translation. */}
                    {!guarded && (scratchOnly ? (k.dirty_untracked_paths ?? []).join(', ') : pruneVerdictLabel(k.code))}
                  </span>
                </label>
              )
            })}
            {hasForceSelected && <p style={{ fontSize: 11, color: 'var(--danger)', margin: '6px 0 0' }}>{i18nT('pages.devFleetPage.force_remove_warning')}</p>}
          </div>
        )}
        {pruneDialog.candidates.length === 0 && !pruneDialog.kept.length && <p style={{ fontSize: 12, color: 'var(--muted)' }}>{i18nT('pages.devFleetPage.no_candidates_found')}</p>}
        <p style={{ fontSize: 11, color: 'var(--muted)', margin: '8px 0 0' }}>{i18nT('pages.devFleetPage.removes_worktrees_and_stops_pods_cannot_be_undon')}</p>
      </div>
    </Modal>
  )
}

/** The prune progress checklist, one row per selected worktree. */
export function DevFleetPruneProgress({
  pruneProgress, setPruneProgress,
}: {
  pruneProgress: PruneProgress
  setPruneProgress: Dispatch<SetStateAction<PruneProgress | null>>
}) {
  const pruneDone = pruneProgress != null && !pruneProgress.running
  return (
    <Modal
      open={true}
      onClose={() => { if (pruneDone) setPruneProgress(null) }}
      title={pruneDone ? i18nT('pages.devFleetPage.prune_complete') : i18nT('pages.devFleetPage.pruning_worktrees')}
      maxWidth={460}
      footer={pruneDone ? <Btn onClick={() => setPruneProgress(null)}>{i18nT('pages.devFleetPage.close')}</Btn> : undefined}
    >
      <div style={{ fontSize: 12 }}>
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          {pruneDone ? i18nT('pages.devFleetPage.finished') : i18nT('pages.devFleetPage.removing')} {pruneProgress.done}/{pruneProgress.total}
        </div>
        <div role="list" style={{ display: 'flex', flexDirection: 'column', maxHeight: 320, overflowY: 'auto' }}>
          {pruneProgress.names.map((nm) => {
            const it = pruneProgress.items[nm] || { status: 'pending', error: null }
            const meta = PRUNE_STATUS_META[it.status] || PRUNE_STATUS_META.pending
            return (
              <div key={nm} role="listitem" data-testid={`prune-item-${nm}`} data-status={it.status} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', borderBottom: '1px solid var(--border)' }}>
                <span style={{ width: 14, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                  {meta.kind === 'spin'
                    ? <LoaderCircle size={12} className="lucide-inline animate-spin" style={{ color: 'var(--muted)' }} />
                    : meta.kind === 'done'
                      ? <Check size={13} style={{ color: 'var(--ok)' }} />
                      : meta.kind === 'failed'
                        ? <X size={13} style={{ color: 'var(--danger)' }} />
                        : <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--muted)', opacity: 0.4 }} />}
                </span>
                <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flexShrink: 0, maxWidth: 200 }}>{nm}</span>
                <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 0, paddingLeft: 8 }}>
                  {it.status === 'failed' && it.error && (
                    // Per-row failure of a server-side removal — nothing to lose, hand-off on.
                    <ErrorNotice message={it.error} variant="inline" askAgent className="min-w-0" testId={`prune-item-error-${nm}`} />
                  )}
                  <Badge variant={meta.kind === 'done' ? 'ok' : meta.kind === 'failed' ? 'err' : 'muted'} className="text-[10.5px] px-1.5 py-0">{pruneStatusLabel(it.status)}</Badge>
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </Modal>
  )
}
