import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Download, RefreshCw } from 'lucide-react'

import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'
import { settingsPath } from './settingsPath'
import type { UpdateState } from '../hooks/useUpdateSubscription'

/**
 * Top-bar update pill: the persistent, passive home for update state.
 *
 * Absent while there is nothing to say — it renders only when an update
 * exists (the same combined desktop-or-gateway fact the nav dot uses), so its
 * mere presence is the signal, the way a browser's update pill works. It then
 * carries the lifecycle so the modal does not have to stay open for it:
 * "Update" while one is available, live percent while the desktop build
 * downloads, "Restart to update" once it is staged.
 *
 * Clicking always deep-links to Settings → About — the one surface with every
 * update action — rather than performing anything itself: a top-bar control
 * that installs software on a single click is a misclick hazard.
 *
 * Deliberately NOT muted by a snooze or skip: those silence the proactive
 * interruption, and a user who skipped a version still deserves a quiet,
 * visible way back in.
 */
export default function UpdatePill() {
  const navigate = useNavigate()
  const updateAvailable = useAppSelector(
    s => s.dashboard.status?.update_available === true || s.dashboard.desktopUpdateAvailable
  )
  const { data: desktop } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false, // populated by useUpdateSubscription (App.tsx)
    staleTime: Infinity,
  })

  if (!updateAvailable) return null

  // Status labels, not action labels: the pill only NAVIGATES (to Settings ›
  // About), so a label like "Restart to update" would promise an action the
  // click does not perform — a platform-convention trap Chrome/VS Code users
  // fall into once per release.
  let label = i18nT('components.updatePill.update_available')
  let Icon = Download
  if (desktop?.state === 'downloading') {
    const pct = typeof desktop.percent === 'number' ? Math.round(desktop.percent) : null
    label = pct === null
      ? i18nT('components.updatePill.downloading')
      : i18nT('components.updatePill.downloading_percent', { percent: pct })
  } else if (desktop?.state === 'downloaded') {
    label = i18nT('components.updatePill.update_ready')
    Icon = RefreshCw
  }

  return (
    <button
      type="button"
      data-testid="update-pill"
      className="flex items-center gap-1.5 h-7 px-2.5 rounded-xl shrink-0 cursor-pointer text-[12px] whitespace-nowrap border border-accent/30 bg-accent-subtle text-accent hover:opacity-90 transition-opacity"
      onClick={() => navigate(settingsPath({ tab: 'about' }))}
      title={i18nT('components.updatePill.open_update_settings')}
      aria-label={i18nT('components.updatePill.open_update_settings')}
    >
      <Icon size={13} className="lucide-inline" />
      {/* The text label yields at narrow widths (the icon + aria-label carry
          the meaning) so the pill cannot push the notification control out of
          a 320px top bar. */}
      <span className="hidden sm:inline">{label}</span>
    </button>
  )
}
