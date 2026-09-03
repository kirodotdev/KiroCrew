/** <SettingsLink> — inline link into a Settings tab / sub-page / setting.
 *
 *  The hand-authored counterpart to the registry-driven deep links
 *  (settingsRoute for palette/search entries, SettingRef for config-key
 *  chips): any prose or empty state that says "configure this in Settings"
 *  renders this instead of a hand-written `/settings/...` string, so the
 *  route shape ( path segments + `highlight` query ) has one write path.
 *
 *  Carries no strings of its own — the caller passes localized children.
 */
import { Link } from 'react-router-dom'
import type { ComponentProps, ReactNode } from 'react'
import { settingsPath } from './settingsPath'
import type { SettingsTarget } from './settingsPath'

export interface SettingsLinkProps
  extends SettingsTarget,
    Omit<ComponentProps<typeof Link>, 'to'> {
  children: ReactNode
}

export function SettingsLink({
  tab,
  sub,
  highlight,
  params,
  className,
  children,
  ...linkProps
}: SettingsLinkProps) {
  return (
    <Link
      to={settingsPath({ tab, sub, highlight, params })}
      className={className ?? 'text-accent hover:underline'}
      {...linkProps}
    >
      {children}
    </Link>
  )
}
