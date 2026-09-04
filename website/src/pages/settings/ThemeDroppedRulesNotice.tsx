import { TriangleAlert } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import type { OverridesDropReport } from '../../hooks/useTheme'

/**
 * Settings-side surface for rules the runtime scoper removed from the active
 * theme's overrides.css. The scoper silently drops rules the theming
 * contract disallows, so the active theme can render unlike what its author
 * wrote with the only other signal in the console — a channel no dashboard user
 * has open. This lives directly under the theme selector, where a user looks
 * when their theme seems wrong.
 *
 * Condition-derived, not dismissal-based: it renders while the active theme's
 * report is non-empty and disappears on its own once the pack is fixed and
 * re-installed — the vanishing IS the author's "fixed" confirmation. Rule names
 * are untrusted pack text rendered strictly as text nodes, never HTML.
 */
export function ThemeDroppedRulesNotice({ report }: { report: OverridesDropReport }) {
  return (
    <div role="status" className="flex items-start gap-2 rounded-md border border-border bg-warn-subtle px-3 py-2">
      <TriangleAlert size={14} className="shrink-0 mt-[2px] text-warn" aria-hidden />
      <div className="flex flex-col gap-1 min-w-0">
        <span className="text-[12px] font-medium text-text">{i18nT('pages.settings.displayPanel.theme_styles_ignored_title')}</span>
        <span className="text-[12px] text-muted leading-relaxed">{i18nT('pages.settings.displayPanel.theme_styles_ignored_body')}</span>
        <code className="text-[11px] font-mono text-muted break-all">{report.rules.join(' · ')}</code>
        <a
          href="https://github.com/kirodotdev/KiroCrew/blob/main/website/docs/theming-contract.md"
          target="_blank" rel="noopener noreferrer"
          className="text-[12px] text-accent hover:underline w-fit"
        >{i18nT('pages.settings.displayPanel.theme_styles_ignored_link')}</a>
      </div>
    </div>
  )
}
