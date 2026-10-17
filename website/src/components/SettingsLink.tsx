/** <SettingsLink> — inline link into a Settings tab / sub-page / setting.
 *
 *  The hand-authored counterpart to the registry-driven deep links
 *  (settingsRoute for palette/search entries, SettingRef for config-key
 *  chips): any prose or empty state that says "configure this in Settings"
 *  renders this instead of a hand-written `/settings/...` string, so the
 *  route shape ( path segments + `highlight` query ) has one write path.
 *
 *  It is also one NAVIGATION SURFACE: an in-app route change that unmounts the
 *  page under it. The page on screen may hold a draft (a prompt being edited,
 *  a token half-typed), and the leave-guard channel is opt-in per surface —
 *  see NavigationLeaveGuard.tsx — so this component asks it ONCE, here, for
 *  every consumer, through `useGuardedLeave`. A modified or non-primary click
 *  (Cmd/Ctrl/Shift/Alt, middle button) opens the href in a new tab and
 *  unmounts nothing, so it is left to the browser and never asked.
 *
 *  Carries no strings of its own — the caller passes localized children.
 */
import { Link, useNavigate } from 'react-router-dom'
import type { ComponentProps, MouseEvent, ReactNode } from 'react'
import { settingsPath } from './settingsPath'
import type { SettingsTarget } from './settingsPath'
import { useGuardedLeave } from './NavigationLeaveGuard'

export interface SettingsLinkProps
  extends SettingsTarget,
    Omit<ComponentProps<typeof Link>, 'to' | 'onClick'> {
  /**
   * Optional because the react-i18next <Trans> idiom passes the element
   * self-closing — `components={[<SettingsLink key="l" tab="…" />]}` with
   * `<0>…</0>` in the catalog value (the englishIdentity gate rejects named
   * closing tags, and `<link>` is an HTML void element) — and react-i18next
   * injects the translated fragment as children at render time.
   */
  children?: ReactNode
  /**
   * Runs for an unmodified primary click the page has allowed, just before the
   * router navigates — the hook for a host that must change its own state so
   * the navigation is visible (a viewport returning to its Local tab before
   * the local SPA underneath it moves). Never runs for a modified click or
   * when the leave guard vetoes, so a host cannot act on a navigation that is
   * not going to happen.
   */
  onPlainClick?: () => void
}

function isPlainPrimaryClick(e: MouseEvent<HTMLAnchorElement>): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
}

export function SettingsLink({
  tab,
  sub,
  highlight,
  params,
  className,
  children,
  onPlainClick,
  ...linkProps
}: SettingsLinkProps) {
  const to = settingsPath({ tab, sub, highlight, params })
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!isPlainPrimaryClick(e)) return
    // The router's own click handling is replaced by the guarded form: nothing
    // below runs unless the page agreed to be left (or the target is the
    // address already on screen, which unmounts nothing).
    e.preventDefault()
    guardedLeave(() => {
      onPlainClick?.()
      navigate(to)
    }, to)
  }
  return (
    <Link to={to} className={className ?? 'text-accent hover:underline'} onClick={onClick} {...linkProps}>
      {children}
    </Link>
  )
}
