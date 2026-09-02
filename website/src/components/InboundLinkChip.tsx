import { ArrowLeftRight } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { useAppSelector } from '../store'
import { ChannelBrandIcon } from './ChannelBrandIcon'

/**
 * Header chip for a session that is being DRIVEN from another channel.
 *
 * A `direction: 'both'` link (created by an in-channel `!sessions` pick) means
 * messages sent in that channel arrive in this session. That is otherwise
 * invisible — the session looks like any other dashboard tab — so it gets a
 * persistent chip rather than living only in a menu the user has to open.
 * `origin` and one-way `out` links deliberately render nothing here: they carry
 * no surprise.
 *
 * INFORMATION ONLY: there is no action on this chip. Connecting and
 * disconnecting a channel happens in exactly one place — the session menu's
 * single row per channel — and this chip is not a second, contradictory control.
 * It previously carried a "Release" button that hard-unlinked the binding, which
 * was three separate problems: it severed a connection the menu can only mute,
 * so the two controls disagreed about what disconnecting means; it was the last
 * destructive confirmation in the surface; and its copy named the machinery
 * (`release`, `two-way`, `!sessions`) that this vocabulary cleanup removes.
 *
 * The chip stays visible when the channel is disconnected, and that is correct
 * rather than an oversight: a disconnect stops OUTBOUND delivery only, so
 * messages sent there still arrive here — which is exactly what this chip
 * claims, and exactly what makes replying there resume the conversation.
 */
export default function InboundLinkChip({ slotKey }: { slotKey?: string }) {
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  const inbound = slot?.links?.find(link => link.direction === 'both')

  if (!slotKey || !inbound) return null

  return (
    <span className="pointer-events-auto inline-flex items-center gap-1.5 rounded-md border border-border bg-accent-subtle px-2 py-0.5 text-[11px] text-muted">
      <ArrowLeftRight size={11} className="shrink-0 text-accent" aria-hidden />
      <ChannelBrandIcon channel={inbound.channel} size={11} />
      <span className="truncate max-w-[22ch]">
        {i18nT('components.inboundLinkChip.driven_from', { label: inbound.label })}
      </span>
    </span>
  )
}
