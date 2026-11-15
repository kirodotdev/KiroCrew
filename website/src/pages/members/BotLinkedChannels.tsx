import { useRef, useState } from 'react'
import { ChevronDown, Link2, Pause } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { ConfiguredChannelTarget } from '../../types'
import { useAppSelector } from '../../store'
import { slotRowByKey } from '../../store/slotIndex'
import { ChannelBrandIcon } from '../../components/ChannelBrandIcon'
import { useLabelTip } from '../../components/InstantTip'
import LinkedSurfacesSection from '../../components/LinkedSurfacesSection'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import HeaderControl from './HeaderControl'

/**
 * The Bot thread's linked-channel control. A Bot's own chat has no session
 * menu, so once it was linked to Slack (or any channel) nothing on the page
 * could pause or unlink it. This opens the session menu's own channel rows
 * (`LinkedSurfacesSection`) under a header chip, so the verbs and what they
 * do are the same component everywhere.
 *
 * The chip stays while the Bot chat has a link OR a channel it can connect to:
 * the rows' Connect offers are how an unlinked chat links again, so hiding the
 * chip with the last link would make an Unlink a one-way door. With no link
 * and nothing configured to connect to there is nothing to offer, and no chip
 * (unless the read of what can be connected failed: then the menu says so).
 *
 * shortcut: the chip names the first link only; a Bot linked to two channels
 * still gets every row in the menu. Name them all if that becomes common.
 */
export default function BotLinkedChannels({ slotKey, inBar }: { slotKey: string; inBar: boolean }) {
  const { t } = useTranslation()
  const link = useAppSelector(s => slotRowByKey(s.dashboard.slots, slotKey))?.links?.[0]
  // The same query, key and shape the rows' offers read, so the two share one
  // cache entry and agree on whether anything can be connected.
  const { data: targets = [], errorUpdatedAt, dataUpdatedAt } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(result => (
      Array.isArray(result) ? result as ConfiguredChannelTarget[] : []
    )),
    refetchInterval: 30_000,
  })
  // A failed target read keeps the chip too: its menu carries the rows' own
  // load-error notice, which a hidden chip would make unreachable. Read as
  // "the last outcome was an error", not `isError`: that flag drops while the
  // rows' own mount refetches, which would hide the chip, unmount the rows and
  // start the loop again.
  const targetsFailed = errorUpdatedAt > dataUpdatedAt
  const visible = !!link || targets.length > 0 || targetsFailed
  const label = link
    ? t(link.paused ? 'pages.membersPage.linked_to_paused' : 'pages.membersPage.linked_to', { label: link.label })
    : t('pages.membersPage.connect_channel')
  const tip = useLabelTip(label)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  // The tip names the chip; once its menu is open the rows say it, and a tip
  // left up would paint over the first row.
  const [open, setOpen] = useState(false)
  if (!visible) {
    // A row that keeps its menu open on select (Unlink does) never closes the
    // menu when the chip goes: drop the open state with the chip, or the menu
    // would pop open by itself when the chip comes back.
    if (open) setOpen(false)
    return null
  }
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <HeaderControl
          ref={triggerRef}
          inBar={inBar}
          chipClassName="glass-hover flex items-center justify-center gap-0.5 h-8 px-2 rounded-full shrink-0 text-text cursor-pointer"
          barClassName="mc-touch-hit flex items-center gap-0.5 p-2 rounded-md shrink-0 text-text hover:text-text-strong hover:bg-bg-hover cursor-pointer bg-transparent border-none"
          aria-label={label}
          data-testid="member-linked-channels"
          data-paused={link?.paused ? '' : undefined}
          {...tip.handlers}
        >
          {link
            ? <ChannelBrandIcon channel={link.channel} size={inBar ? 16 : 14} />
            : <Link2 size={inBar ? 16 : 14} className="shrink-0 text-muted" aria-hidden />}
          {/* Paused replies read differently with the menu closed, too. */}
          {link?.paused && <Pause size={10} className="shrink-0 text-muted" aria-hidden data-testid="member-linked-channels-paused" />}
          <ChevronDown size={11} className="shrink-0 text-muted" aria-hidden />
          {!open && tip.bubble}
        </HeaderControl>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="min-w-[220px]"
        onCloseAutoFocus={event => {
          // If the chip left with the menu (an Unlink with nothing left to
          // connect to), Radix would restore focus to a detached trigger and
          // drop it on `document.body`. Hand it to the identity pill instead,
          // or the panel toggle while a docked Profile has the pill's place.
          if (triggerRef.current?.isConnected) return
          const target = document.querySelector<HTMLElement>(
            '[data-testid="member-identity-pill"], [data-testid="member-panel-toggle"]',
          )
          if (!target?.isConnected) return
          event.preventDefault()
          target.focus()
        }}
      >
        <LinkedSurfacesSection slotKey={slotKey} variant="dropdown" withoutSessionMenu />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
