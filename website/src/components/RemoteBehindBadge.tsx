/**
 * "Behind: remote X, local Y" — the exact-version skew between a connected crew
 * and this machine, shown on the crew's Remote Crew row and in its crew-window
 * header.
 *
 * Read-only by design: it states which builds the two ends run and nothing
 * else. The gateway decides `behind` (an exact, pre-release-aware compare), so
 * this component never orders version strings itself and renders nothing when
 * the crew is current, ahead, or its version is unknown.
 */
import { ArrowDownCircle } from 'lucide-react'
import { Badge } from './ui'
import { i18nT } from '../i18n/t'

export default function RemoteBehindBadge({
  behind,
  remote,
  local,
  className,
}: {
  behind: boolean | undefined
  remote: string
  local: string
  className?: string
}) {
  if (!behind || !remote || !local) return null
  const label = i18nT('components.remoteBehindBadge.label', { remote, local })
  return (
    <Badge
      variant="warn"
      className={className}
      title={i18nT('components.remoteBehindBadge.hint', { remote, local })}
      data-testid="remote-behind-badge"
    >
      <ArrowDownCircle className="lucide-inline" aria-hidden /> {label}
    </Badge>
  )
}
