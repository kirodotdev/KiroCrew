import { memo } from 'react'
import { Pause } from 'lucide-react'

import { useCrewMode } from '../../hooks/usePreviewFlag'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

/**
 * The main chat's "You paused the crew" row (`meta.kind === 'crew_paused_notice'`,
 * written by `src/kiro_crew/dashboard/handlers/crew_pause.py` when the person
 * presses Pause all). `meta.stopped` is how many crew members the pause stopped.
 * The row's English `content` is only a fallback for channel mirrors and SSE
 * readers; here the copy comes from the catalog. Same visual grammar as
 * `CrewDispatchNoticeCard`, and like it, nothing at all while the Ultra Mode
 * preview is off in this browser.
 */
export default memo(function CrewPausedNoticeCard({ stopped }: { stopped: number }) {
  const crewMode = useCrewMode()
  useLanguageGeneration()
  if (!crewMode) return null
  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="crew-paused-notice-card"
    >
      <div className="flex items-start gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">
        <Pause size={13} className="lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)] crew-tone-paused" aria-hidden="true" />
        <span className="min-w-0 break-words" data-testid="crew-paused-notice-text">
          {i18nT('components.crewMode.ultra_paused_notice', { count: stopped })}
        </span>
      </div>
    </div>
  )
})
