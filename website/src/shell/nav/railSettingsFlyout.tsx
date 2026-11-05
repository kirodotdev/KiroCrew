import { useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Settings2, Smartphone } from 'lucide-react'

import { Popover, PopoverAnchor, PopoverContent } from '../../components/ui/popover'
import { i18nT } from '../../i18n/t'
import { NavItem } from './navRow'

/** Window event that opens the rail customize modal from outside the rail groups. */
export const OPEN_RAIL_CUSTOMIZE_EVENT = 'mc:open-rail-customize'
export const openRailCustomize = () => window.dispatchEvent(new Event(OPEN_RAIL_CUSTOMIZE_EVENT))

interface FlyoutRoute { path: string; label: string; icon: ReactNode }

function FlyoutRow({ icon, label, onClick, active }: { icon: ReactNode; label: string; onClick: () => void; active?: boolean }) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-sm bg-transparent border-none cursor-pointer ${active ? 'bg-bg-hover text-text' : 'text-text hover:bg-bg-hover'}`}
    >
      <span className="w-4 h-4 flex items-center justify-center shrink-0 text-muted">{icon}</span>
      <span className="flex-1 min-w-0 truncate">{label}</span>
    </button>
  )
}

/**
 * The rail's one permanent row: Settings, with a flyout menu holding the
 * config destinations that used to be separate rail rows. Clicking the row
 * opens the menu beside the rail (Settings is its first entry); clicking it
 * again, Esc or clicking outside closes it. The flyout is a Radix popover, portaled to the overlay root
 * so the rail's `overflow-hidden` cannot clip it.
 */
export function RailSettingsFlyout({ settings, customize, collapsed, activePath, badge, phone, onNavigate }: {
  settings: FlyoutRoute
  customize: FlyoutRoute
  collapsed: boolean
  activePath: string
  badge?: ReactNode
  /** Present when phone connect can render. */
  phone?: { open: () => void }
  onNavigate?: () => void
}) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const anchor = useRef<HTMLDivElement>(null)

  const isOn = (path: string) => activePath === path || activePath.startsWith(path + '/')
  const go = (path: string) => { setOpen(false); onNavigate?.(); navigate(path) }

  return (
    <Popover open={open} onOpenChange={setOpen} modal={false}>
      <PopoverAnchor asChild>
        <div ref={anchor}>
          <NavItem
            path={settings.path}
            label={settings.label}
            icon={settings.icon}
            active={open || isOn(settings.path) || isOn(customize.path)}
            collapsed={collapsed}
            // Clicking the gear opens the menu; Settings itself is its first row.
            onClickOverride={() => setOpen(o => !o)}
            badge={badge}
          />
        </div>
      </PopoverAnchor>
      <PopoverContent
        side="right"
        align="end"
        sideOffset={10}
        variant="list"
        className="w-60"
        role="menu"
        aria-label={settings.label}
        // A press on the gear itself is its own toggle, not an outside click.
        onInteractOutside={e => { if (anchor.current?.contains(e.target as Node)) e.preventDefault() }}
      >
        <FlyoutRow icon={settings.icon} label={settings.label} active={isOn(settings.path)} onClick={() => go(settings.path)} />
        <FlyoutRow icon={customize.icon} label={customize.label} active={isOn(customize.path)} onClick={() => go(customize.path)} />
        <div aria-hidden="true" className="my-1 h-px bg-border" />
        {phone && <FlyoutRow icon={<Smartphone size={16} />} label={i18nT('app.connect_your_phone')} onClick={() => { setOpen(false); phone.open() }} />}
        <FlyoutRow icon={<Settings2 size={16} />} label={i18nT('nav.rail_customize')} onClick={() => { setOpen(false); openRailCustomize() }} />
      </PopoverContent>
    </Popover>
  )
}
