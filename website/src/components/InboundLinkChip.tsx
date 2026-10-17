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
 * The chip carries no control of its OWN: connecting, pausing and unlinking a
 * channel happen in exactly one place — the session menu's rows for that
 * channel — and the chip is the same menu's trigger, never a second,
 * contradictory control. It previously carried
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
 * has just clicked Pause and reads "Driven from Discord DM" unchanged takes
 * the chip for stale. So the three states the binding can be in each read
 * differently — linked ("Driven from X"), disconnected but still bound
 * ("Driven from X · replies paused"), and unlinked (no chip, because there is
 * no two-way link left to describe).
 *
 * Both chip states are ONE affordance: a menu trigger over the session menu's
 * own rows for that channel. The PAUSED chip is the one that invites repair — a
 * reader who sees "replies paused" clicks it hoping to resume — and a chip that
 * names a problem and
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
 * what the click offers. The LIVE chip is the same trigger, opening the same
 * rows (`Pause replies to X`, `Unlink from X`): a button beside a plain span
 * that looked identical gave one element two behaviours a reader could not
 * tell apart before clicking, and a chip that is visibly interactive in one
 * state and inert in the other is the dead click again, on the other state.
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

  // ONE affordance for both states. The chip is a menu trigger whether the
  // replies are flowing or paused: a paused chip that was a button beside a
  // live chip that was a plain span gave one element two behaviours a reader
  // could not tell apart before clicking ("the same chip sometimes opens
  // something and sometimes does nothing"). The menu already carries the live
  // row's actions — `Pause replies to X` and `Unlink from X` — so opening it
  // from the live chip adds no second control, only the one place those verbs
  // live. What differs between the states is what the chip SAYS (its label
  // names the pause) and what the hint promises the click offers.
  const hint = inbound.paused
    ? i18nT('components.inboundLinkChip.driven_from_paused_hint', { label: inbound.label })
    : i18nT('components.inboundLinkChip.driven_from_hint', { label: inbound.label })

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={`${chipClass} cursor-pointer hover:text-text hover:border-accent transition-colors`}
          title={hint}
        >
          {body}
          <ChevronDown size={11} className="shrink-0" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      {/* The session menu's rows for THIS channel: the same `LinkedSurfacesSection`
        * the sidebar menu and the header dropdown render, so a click here does
        * exactly what a click there does — scoped to the channel the chip names,
        * because its hint promises to pause/resume or unlink Discord DM and a
        * menu that also offered Slack or Telegram would not match its trigger.
        * The rows keep the menu open on select (the row IS the state display);
        * a pause or a resume redraws the chip's label in place, and an unlink
        * removes the chip and its menu together. */}
      <DropdownMenuContent align="start" className="min-w-[220px]">
        <LinkedSurfacesSection slotKey={slotKey} variant="dropdown" channel={inbound.channel} />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
