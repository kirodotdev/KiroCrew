/**
 * What an app's manifest `desktop` block asks the desktop shell for, as the trust
 * prompt shows it before the user says yes.
 *
 * The shell hosts these windows only in Kiro Crew Desktop, and only the shapes the
 * manifest declares (`apps/desktop_manifest.py`), so the box lists the shapes, not
 * the app's own words about them. A manifest with no `desktop` block shows nothing.
 */
import { Monitor } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import type { DesktopRequest } from './desktopRequest'

export default function TrustAppDesktopBox({ request }: { request?: DesktopRequest }) {
  if (!request) return null
  const line = 'text-text leading-relaxed'
  return (
    <div
      data-testid="trust-desktop-box"
      className="flex flex-col gap-1.5 rounded-lg border border-border bg-[color-mix(in_srgb,var(--card)_88%,transparent)] px-3.5 py-3"
    >
      <div className="flex items-center gap-2 font-semibold text-text">
        <Monitor size={14} className="shrink-0" />
        <span>{i18nT('components.appstore.trustAppModal.desktop_heading')}</span>
      </div>
      {request.spansDisplays && <p className={line}>{i18nT('components.appstore.trustAppModal.desktop_spans_displays')}</p>}
      {request.overlay && <p className={line}>{i18nT('components.appstore.trustAppModal.desktop_overlay')}</p>}
      {request.panel && <p className={line}>{i18nT('components.appstore.trustAppModal.desktop_panel')}</p>}
      {request.background && <p className={line}>{i18nT('components.appstore.trustAppModal.desktop_background')}</p>}
      {request.shortcuts && <p className={line}>{i18nT('components.appstore.trustAppModal.desktop_shortcuts')}</p>}
    </div>
  )
}
