import { ArrowLeftRight, ChevronDown } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { useAppSelector } from '../store'
import { ChannelBrandIcon } from './ChannelBrandIcon'
import LinkedSurfacesSection from './LinkedSurfacesSection'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from './ui/dropdown-menu'

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
 * The LIVE chip is information only: connecting and disconnecting a channel
 * happens in exactly one place — the session menu's single row per channel —
 * and this chip is not a second, contradictory control. It previously carried
 * a "Release" button that hard-unlinked the binding, which was three separate
 * problems: it severed a connection the menu can only mute, so the two controls
 * disagreed about what disconnecting means; it was the last destructive
 * confirmation in the surface; and its copy named the machinery (`release`,
 * `two-way`, `!sessions`) that this vocabulary cleanup removes.
 *
 * The chip stays visible when the channel is disconnected, and that is correct
 * rather than an oversight: a disconnect stops OUTBOUND delivery only, so
 * messages sent there still arrive here — which is exactly what this chip
 * claims, and exactly what makes replying there resume the conversation. It
 * must not say so in the same words as the connected state, though: a user who
 * has just clicked Disconnect and reads "Driven from Discord DM" unchanged takes
 * the chip for stale. So the three states the binding can be in each read
 * differently — linked ("Driven from X"), disconnected but still bound
 * ("Driven from X · replies paused"), and unlinked (no chip, because there is
 * no two-way link left to describe).
 *
 * The PAUSED chip is the one that invites repair — a reader who sees "replies
 * paused" clicks it hoping to resume — and a chip that names a problem and
 * answers a click with nothing is a dead click at the moment of need. A
 * hover-only `title` did not cure that: it is delayed, absent on touch, and
 * invisible to a screen reader that never hovers. So the paused chip IS a
 * control: a menu trigger that opens the session menu's own Linked surfaces
 * rows — `Resume replies to X` and `Unlink from X` — right under the
 * chip. Not a second control, the same one, reached from where the problem is
 * read: the rows are the one component that connects, disconnects and unlinks,
 * so the chip cannot disagree with the menu about what any verb means. The
 * chevron is the visible cue that it opens something; the paused fact stays
 * its label, and the `title` remains as the pointer-and-description hint of
 * what the click offers.
 */
export default function InboundLinkChip({ slotKey }: { slotKey?: string }) {
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  const inbound = slot?.links?.find(link => link.direction === 'both')

  if (!slotKey || !inbound) return null

  const chipClass = 'pointer-events-auto inline-flex items-center gap-1.5 rounded-md border border-border bg-accent-subtle px-2 py-0.5 text-[11px] text-muted'
  const body = (
    <>
      <ArrowLeftRight size={11} className="shrink-0 text-accent" aria-hidden />
      <ChannelBrandIcon channel={inbound.channel} size={11} />
      <span className="truncate max-w-[40ch]">
        {inbound.paused
          ? i18nT('components.inboundLinkChip.driven_from_paused', { label: inbound.label })
          : i18nT('components.inboundLinkChip.driven_from', { label: inbound.label })}
      </span>
    </>
  )

  if (!inbound.paused) {
    return <span className={chipClass}>{body}</span>
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={`${chipClass} cursor-pointer hover:text-text hover:border-accent transition-colors`}
          title={i18nT('components.inboundLinkChip.driven_from_paused_hint', { label: inbound.label })}
        >
          {body}
          <ChevronDown size={11} className="shrink-0" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      {/* The session menu's rows, unchanged: the same `LinkedSurfacesSection`
        * the sidebar menu and the header dropdown render, so a click here does
        * exactly what a click there does. The rows keep the menu open on select
        * (the row IS the state display); once the resume lands the chip reads
        * live again and this trigger gives way to the plain chip. */}
      <DropdownMenuContent align="start" className="min-w-[220px]">
        <LinkedSurfacesSection slotKey={slotKey} variant="dropdown" />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
