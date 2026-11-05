import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'

import type { getThemeBranding } from '../../themeBranding'
import { useGuideRevealScope } from '../../guide/GuideRevealScope'
import { i18nT } from '../../i18n/t'
import { RailHeaderGlyph } from '../nav/railChrome'

/**
 * Desktop brand + rail expand/collapse toggle, at the top bar's left edge
 * ahead of Back/Forward. The brand (theme-aware logo plus the bot-name
 * wordmark, identical in both rail states) is a mark, not a control; the toggle is
 * its own panel-left button beside it. The phone drawer keeps RailBrandToggle.
 */
export function TopBarRailToggle({ collapsed, toggleNav, avatar, branding, botName }: {
  collapsed: boolean
  toggleNav: () => void
  avatar: string
  branding: ReturnType<typeof getThemeBranding>
  botName: string
}) {
  // The rail's rows are reachable to a guide only while it is expanded.
  useGuideRevealScope('shell.nav-rail', !collapsed)
  const label = collapsed ? i18nT('app.expand_sidebar') : i18nT('app.collapse_sidebar')
  const split = botName.lastIndexOf(' ')
  return (
    <>
      <span data-testid="topbar-brand" className="h-7 mr-1 inline-flex items-center gap-2 px-1 shrink-0 select-none">
        <RailHeaderGlyph avatar={avatar} boxClass={branding?.logoClass ?? 'w-5 h-5'} iconSize={16} />
        {/* Same in both rail states, so the toggle beside it never moves. */}
        <span className="text-[12px] font-bold tracking-[.14em] uppercase whitespace-nowrap">
          {/* Last word of the bot name carries the accent, as in the rail brand. */}
          {split > 0 ? (
            <>
              <span className="text-muted">{botName.slice(0, split + 1)}</span>
              <span className="text-accent/90">{botName.slice(split + 1)}</span>
            </>
          ) : (
            <span className="text-muted">{botName}</span>
          )}
        </span>
      </span>
      <button
        type="button"
        onClick={toggleNav}
        title={label}
        aria-label={label}
        aria-expanded={!collapsed}
        className="w-7 h-7 mr-0.5 inline-flex items-center justify-center rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover shrink-0"
      >
        {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
      </button>
    </>
  )
}
