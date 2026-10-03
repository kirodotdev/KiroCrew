import { RefreshCw } from 'lucide-react'
import { i18nT } from '../i18n/t'
import type { ChatSlot } from '../types'
import { Badge } from './ui'

/**
 * The tooltip: what changed (display-safe labels from the gateway) and the remedy.
 * The header badge sits inside the session-menu trigger, so its tooltip says a
 * click opens the menu; the `compact` sidebar mark does not open it (a click on
 * the row opens the chat), so its tooltip names the remedy alone.
 */
export function staleConfigTooltip(inputs: string | undefined, compact = false): string {
  if (compact) {
    return inputs
      ? i18nT('components.staleConfigBadge.tooltip_compact', { inputs })
      : i18nT('components.staleConfigBadge.tooltip_compact_generic')
  }
  return inputs
    ? i18nT('components.staleConfigBadge.tooltip', { inputs })
    : i18nT('components.staleConfigBadge.tooltip_generic')
}

/**
 * The "stale config" mark on a chat whose agent process runs on config changed
 * since it started (`slot.config_stale`, computed by the gateway). Nothing is
 * relaunched for it: the tooltip says the session needs a Reload to apply the
 * change. `compact` is the sidebar row's icon-only form, directly after the
 * agent name on the row's meta line; the full form is the chat header's labelled badge, rendered
 * inside the session-menu trigger so a click opens the menu with Reload in it.
 */
export default function StaleConfigBadge({
  slot,
  compact = false,
}: {
  slot: Pick<ChatSlot, 'config_stale' | 'config_stale_inputs'> | undefined | null
  compact?: boolean
}) {
  if (!slot?.config_stale) return null
  const tooltip = staleConfigTooltip(slot.config_stale_inputs, compact)
  if (compact) {
    return (
      <span className="text-warn shrink-0" title={tooltip} data-testid="stale-config-badge">
        <RefreshCw size={10} aria-label={tooltip} />
      </span>
    )
  }
  return (
    <Badge
      variant="warn"
      className="text-[11px] px-1.5 py-0 shrink-0"
      title={tooltip}
      aria-label={tooltip}
      data-testid="stale-config-badge"
    >
      <RefreshCw size={11} aria-hidden="true" />
      {/* Icon alone on a phone-width header, so a long title keeps its room. */}
      <span className="hidden sm:inline">{i18nT('components.staleConfigBadge.label')}</span>
    </Badge>
  )
}
