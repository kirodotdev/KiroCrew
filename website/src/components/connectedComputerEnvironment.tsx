import { useTranslation } from 'react-i18next'
import { Monitor, WifiOff } from 'lucide-react'
import type { SessionEnvironmentProfile } from '../api/client'

/** `SessionEnvironmentProfile.provider` of one of the user's own computers (issue 29949). */
export const CONNECTED_COMPUTER_PROVIDER = 'connected_computer'

export const isConnectedComputer = (provider: string | null | undefined): boolean =>
  provider === CONNECTED_COMPUTER_PROVIDER

/** The picker's second line for a computer: its folder, its id when the name repeats, and "Offline". */
export function ConnectedComputerDetail({ profile }: { profile: SessionEnvironmentProfile }) {
  const { t } = useTranslation()
  const parts = [
    profile.connected ? profile.workdir : '',
    profile.label_is_shared ? t('connectedComputers.deviceId', { id: profile.id }) : '',
    profile.connected ? '' : t('connectedComputers.state.offline'),
  ].filter(Boolean)
  return <span className="truncate text-[12px] text-muted font-mono">{parts.join(' · ')}</span>
}

export function ConnectedComputerGlyph({ size }: { size: number }) {
  return <Monitor size={size} className="shrink-0 opacity-80" aria-hidden />
}

/**
 * The note beside a chat fixed on one of the user's computers when that
 * computer cannot run it: offline (B10), or revoked or removed here, in which
 * case the chat stays bound and never moves elsewhere (B12). Nothing when it is
 * online.
 */
export function BoundComputerNote({ profile, name }: { profile: SessionEnvironmentProfile | undefined; name: string }) {
  const { t } = useTranslation()
  if (profile?.connected) return null
  const text = profile
    ? t('connectedComputers.chat.boundOffline', { name })
    : t('connectedComputers.chat.boundGone')
  return (
    // Wraps rather than truncates: the second half of the line is what to do.
    <span role="status" className="ml-2 inline-flex min-w-0 items-center gap-1 text-[12px] text-muted">
      <WifiOff size={12} className="shrink-0" aria-hidden />
      <span className="min-w-0 break-words">{text}</span>
    </span>
  )
}
