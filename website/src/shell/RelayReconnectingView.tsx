import { Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

/**
 * The calm view a relay Crew window shows while its dropped connection is
 * being retried: a neutral spinner, "Reconnecting…" and two muted lines over
 * the content area, with no error colour. A refusal, an ended session and the
 * desktop app's "currently unavailable" notice are real failures and keep
 * their own surfaces.
 *
 * Mounted for the whole life of a relay Crew window, empty while connected, so
 * its polite live region exists before the drop and announces it: a region
 * does not speak the text it mounts with.
 */
export function RelayReconnectingView({ active }: { active: boolean }) {
  const { t } = useTranslation()
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="relay-reconnecting"
      className={active ? 'absolute inset-0 z-40 flex items-center justify-center bg-bg px-6 text-center' : 'sr-only'}
    >
      {active && (
        <div className="flex max-w-sm flex-col items-center gap-2">
          <Loader2 aria-hidden="true" size={20} strokeWidth={2} className="mb-1 text-muted animate-spin motion-reduce:animate-none" />
          <p className="text-base font-semibold text-text">{t('app.relay_crew_reconnecting')}</p>
          <p className="text-sm text-muted">{t('app.relay_crew_dropped')}</p>
          <p className="text-sm text-muted">{t('app.relay_crew_reconnects_on_its_own')}</p>
        </div>
      )}
    </div>
  )
}
