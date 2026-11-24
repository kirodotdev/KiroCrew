/**
 * The crew switcher's two window-level actions, Add remote crew and Settings
 * (#41685), and the `+` button the top bar shows before any remote crew exists.
 *
 * Both actions open the Remote Crews window (remoteCrewsWindow.tsx) over the crew
 * on screen. Inside a remote crew's pane the window is the HOST's, so the pane's
 * switcher passes `onAction` to relay the request up instead.
 */
import { useState } from 'react'
import { Check, Plus, Settings2 } from 'lucide-react'
import { useLabelTip } from './InstantTip'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from './ui/dropdown-menu'
import { openRemoteCrewsWindow } from './remoteCrewsWindow'
import { i18nT } from '../i18n/t'

/** `add`: open on Add a Crew. Otherwise open on the crew list. */
export type CrewAction = (add: boolean) => void

/** The menu's last group: below every crew, behind a separator. */
export function CrewMenuActions({ onAction = openRemoteCrewsWindow }: { onAction?: CrewAction }) {
  return (
    <>
      <DropdownMenuSeparator />
      <DropdownMenuItem className="gap-2 text-[13px]" data-testid="crew-add-remote" onSelect={() => onAction(true)}>
        <Plus className="lucide-inline shrink-0 text-muted" aria-hidden />
        <span className="flex-1 min-w-0">{i18nT('components.instanceTabBar.add_remote_crew')}</span>
      </DropdownMenuItem>
      <DropdownMenuItem className="gap-2 text-[13px]" data-testid="crew-settings" onSelect={() => onAction(false)}>
        <Settings2 className="lucide-inline shrink-0 text-muted" aria-hidden />
        <span className="flex-1 min-w-0">{i18nT('components.instanceTabBar.crew_settings')}</span>
      </DropdownMenuItem>
    </>
  )
}

/**
 * Before any remote crew exists the bar is just this `+`: Local, then the two
 * actions. Desktop widths only; on a phone the empty header cell stays empty.
 */
export function CrewAddButton() {
  const label = i18nT('components.instanceTabBar.add_remote_crew')
  // It sits in the desktop window's drag band, where a native `title` never
  // paints, so the hint is drawn in the page, and gives way to the open menu.
  const tip = useLabelTip(label)
  const [open, setOpen] = useState(false)
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          {...tip.handlers}
          aria-label={label}
          data-testid="crew-switcher-add"
          className="relative hidden md:flex items-center justify-center h-6 w-6 shrink-0 rounded-md border border-border text-muted transition-colors hover:bg-bg-hover hover:text-text focus-ring"
        >
          <Plus className="lucide-inline shrink-0" aria-hidden />
          {open ? null : tip.bubble}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" aria-label={i18nT('components.instanceTabBar.instances')} className="min-w-[240px]">
        <DropdownMenuItem className="gap-2 text-[13px]" aria-current="page">
          <Check className="lucide-inline shrink-0 text-accent" aria-hidden />
          <span className="flex-1 min-w-0">{i18nT('components.instanceTabBar.local')}</span>
        </DropdownMenuItem>
        <CrewMenuActions />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
