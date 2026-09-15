import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Copy, GitFork, Loader2, User } from 'lucide-react'
import { Btn, Badge } from './ui'
import { api } from '../api/client'
import { timeAgo } from '../utils/timeAgo'
import type { RemoteArtifact } from '../types'

import { i18nT } from '../i18n/t'
// Normalize a best-effort "ISO/epoch string" (the documented, unit-ambiguous
// updated_at contract) to a seconds epoch. ISO strings parse via Date.parse;
// bare numeric strings may be seconds OR milliseconds — a ms value (>= ~1e12,
// i.e. any date past 2001 in ms) would otherwise be read as a far-future
// seconds epoch and render as "just now" forever, so scale it down.
const _MS_EPOCH_THRESHOLD = 1e12
function toTs(raw?: string): number {
  if (!raw) return 0
  const t = Date.parse(raw)
  if (Number.isFinite(t)) return Math.floor(t / 1000)
  const n = Number(raw)
  if (!Number.isFinite(n)) return 0
  return Math.floor(n >= _MS_EPOCH_THRESHOLD ? n / 1000 : n)
}

/** One provider-neutral remote listing row (browse surface). ``provider`` is
 * the registry name the row was listed from — every action routes through
 * /api/remote-artifacts/{provider}/... so no vendor is hardcoded here;
 * ``providerLabel`` is the provider's display_name for copy. */
export default function RemoteArtifactCard({
  artifact,
  provider,
  providerLabel,
  onForked,
  onCloned,
  actionsDisabled = false,
}: {
  artifact: RemoteArtifact
  provider: string
  providerLabel?: string
  onForked?: (slug: string) => void
  onCloned?: (slug: string) => void
  /** Disable Clone/Fork while the row may be STALE — e.g. a full-text query is
   *  re-fetching and keepPreviousData is showing the prior query's rows. Acting
   *  on a stale row would clone/fork an artifact from the previous search. */
  actionsDisabled?: boolean
}) {
  const [forking, setForking] = useState(false)
  const [cloning, setCloning] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()
  const remoteName = providerLabel || provider || 'the remote provider'

  const handleFork = async () => {
    setForking(true)
    setError('')
    try {
      const res = await api.forkRemoteArtifact(provider, artifact.external_id)
      if (res.error) {
        setError(res.error)
      } else {
        onForked?.(res.slug)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('components.remoteArtifactCard.fork_failed'))
    } finally {
      setForking(false)
    }
  }

  // Clone = bidirectional copy (edits sync back to the SAME remote artifact).
  // Offered only when we can positively prove edit/admin (artifact.editable).
  // The provider still validates the push, so this is a shortcut, not a grant.
  const handleClone = async () => {
    setCloning(true)
    setError('')
    try {
      const res = await api.cloneRemoteArtifact(provider, artifact.external_id)
      if (res.error) setError(res.error)
      else onCloned?.(res.slug)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('components.remoteArtifactCard.clone_failed'))
    } finally {
      setCloning(false)
    }
  }

  // Open the in-app read-only viewer (read & comment; the provider's own "open
  // original" link lives on that detail page, so it isn't duplicated here). The
  // route is provider-neutral — provider name + external_id both percent-encoded
  // since a provider-native id can contain "/".
  const openRemote = () =>
    navigate(`/artifacts/remote/${encodeURIComponent(provider)}/${encodeURIComponent(artifact.external_id)}`)

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={openRemote}
      onKeyDown={e => {
        // Only the row itself activates on Enter/Space. Without this guard the
        // same keydown bubbling up from the inner Fork/Clone <button>s would
        // preventDefault() their native activation and open the remote instead,
        // making those buttons keyboard-unreachable (the onClick-only
        // stopPropagation on the button wrapper doesn't cover keydown).
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          openRemote()
        }
      }}
      title={i18nT('components.remoteArtifactCard.open_read_only_viewer', { name: remoteName })}
      className="flex items-start justify-between gap-3 py-2.5 px-3 rounded-lg hover:bg-bg-elevated/60 transition-colors cursor-pointer focus-ring outline-none"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-text truncate">{artifact.title}</span>
          {artifact.visibility && <Badge variant="ok">{artifact.visibility}</Badge>}
          {artifact.tags?.map(t => <Badge key={t} variant="warn">{t}</Badge>)}
        </div>
        <div className="flex items-center gap-3 mt-0.5 text-[12px] text-muted">
          {artifact.owner && (
            <span className="flex items-center gap-1">
              <User className="lucide-inline w-3 h-3" />
              {artifact.owner}
            </span>
          )}
          {typeof artifact.current_version === 'number' && <span>{i18nT('components.remoteArtifactCard.v')}{artifact.current_version}</span>}
          {artifact.updated_at && <span>{timeAgo(toTs(artifact.updated_at))}</span>}
        </div>
        {artifact.snippet && (
          <p className="text-[12px] text-muted mt-0.5 line-clamp-1">{artifact.snippet}</p>
        )}
        {error && <p className="text-[12px] text-danger mt-1">{error}</p>}
      </div>
      {/* Each button stops row-click propagation so it doesn't also open the
          remote (kept on the buttons, not a wrapping div, so the row stays the
          only interactive container). */}
      <div className="flex items-center gap-1.5 shrink-0">
        {artifact.editable && (
          <Btn
            onClick={e => {
              e.stopPropagation()
              handleClone()
            }}
            disabled={cloning || actionsDisabled}
            title={i18nT('components.remoteArtifactCard.clone_into_your_artifacts_a_bidirectional_copy_w')}
          >
            {cloning ? <Loader2 className="lucide-inline w-3.5 h-3.5 animate-spin" /> : <Copy className="lucide-inline w-3.5 h-3.5" />}
            {i18nT('components.remoteArtifactCard.clone')}
          </Btn>
        )}
        <Btn
          onClick={e => {
            e.stopPropagation()
            handleFork()
          }}
          disabled={forking || actionsDisabled}
          title={i18nT('components.remoteArtifactCard.fork_into_your_local_artifacts_your_own_divergen')}
        >
          {forking ? <Loader2 className="lucide-inline w-3.5 h-3.5 animate-spin" /> : <GitFork className="lucide-inline w-3.5 h-3.5" />}
          {i18nT('components.remoteArtifactCard.fork')}
        </Btn>
      </div>
    </div>
  )
}
