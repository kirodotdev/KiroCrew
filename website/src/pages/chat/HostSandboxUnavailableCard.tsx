import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, Copy, RotateCw, X } from 'lucide-react'

import { Btn } from '../../components/ui'
import { useSharedCrewMode, useViewerIsNotOwner } from '../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import { copyToClipboard } from '../../utils/clipboard'

/** `meta.code` of the error row a chat leaves when the Crew's host cannot build an
 *  OS sandbox, so no agent can start (`dashboard/host_sandbox_refusal.py`). */
export const HOST_SANDBOX_UNAVAILABLE = 'host_sandbox_unavailable'

type HostSandboxMeta = { code?: unknown; at?: unknown; retryable?: unknown } | undefined

export const isHostSandboxUnavailable = (meta: ChatMessage['meta']): boolean =>
  (meta as HostSandboxMeta)?.code === HOST_SANDBOX_UNAVAILABLE

/** What Copy details puts on the clipboard: the code and, when the row has it, the
 *  ISO time it failed. Nothing about the host. */
export function hostSandboxDetails(meta: ChatMessage['meta']): string {
  const at = (meta as HostSandboxMeta)?.at
  return typeof at === 'string' && at ? `${HOST_SANDBOX_UNAVAILABLE} ${at}` : HOST_SANDBOX_UNAVAILABLE
}

type CopyState = 'idle' | 'copied' | 'failed'

/**
 * The host-sandbox error row. The machine owner gets `ownerView`, the full
 * diagnostic as every other error row shows it. Anyone else (a Shared Crew
 * teammate) cannot act on the host's setup and gets one line naming whom to ask,
 * plus Copy details. Their copy of the row carries no host detail anyway: the
 * gateway sends a non-owner the plain line (`host_sandbox_refusal.for_non_owner`).
 * Resume stays only on a momentary refusal (`meta.retryable`): otherwise the next
 * start meets the same host and fails the same way. A momentary refusal also
 * says to press Resume rather than to ask the owner, since there
 * is nothing on the host for them to fix.
 */
export function HostSandboxUnavailableCard({ meta, onContinue, continuing, ownerView }: {
  meta: ChatMessage['meta']
  onContinue?: () => void
  continuing?: boolean
  ownerView: ReactNode
}) {
  const { notOwner } = useViewerIsNotOwner()
  const owner = useSharedCrewMode(notOwner).data?.owner_label?.trim()
  const [copy, setCopy] = useState<CopyState>('idle')
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (resetTimer.current) clearTimeout(resetTimer.current) }, [])
  if (!notOwner) return <>{ownerView}</>
  const onCopy = async () => {
    setCopy(await copyToClipboard(hostSandboxDetails(meta)) ? 'copied' : 'failed')
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setCopy('idle'), 1500)
  }
  const canResume = (meta as HostSandboxMeta)?.retryable === true && !!onContinue
  const CopyIcon = copy === 'copied' ? Check : copy === 'failed' ? X : Copy
  return (
    <div
      className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
      data-testid="error-card"
      data-host-sandbox-unavailable="true"
    >
      <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
        {/* The owner label is the owner's own name (owner_name on the backend). */}
        {canResume
          ? i18nT('pages.chat.errorCard.host_sandbox_teammate_retryable')
          : owner
            ? i18nT('pages.chat.errorCard.host_sandbox_teammate_named', { owner })
            : i18nT('pages.chat.errorCard.host_sandbox_teammate_unnamed')}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Btn onClick={onCopy} title={i18nT('pages.chat.errorCard.copy_details_hint')} aria-live="polite" data-testid="error-card-copy-details">
          <CopyIcon size={12} className="lucide-inline shrink-0" aria-hidden="true" />
          {copy === 'copied'
            ? i18nT('pages.chat.errorCard.copy_details_done')
            : copy === 'failed'
              ? i18nT('pages.chat.errorCard.copy_details_failed')
              : i18nT('pages.chat.errorCard.copy_details')}
        </Btn>
        {canResume && (
          <Btn primary onClick={onContinue} busy={continuing} title={i18nT('pages.chat.errorCard.resume_hint')} data-testid="error-card-continue">
            <RotateCw size={12} className="lucide-inline shrink-0" aria-hidden="true" />
            {i18nT('pages.chat.errorCard.resume')}
          </Btn>
        )}
      </div>
    </div>
  )
}
