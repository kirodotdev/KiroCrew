import { Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { OfflineCover } from '../utils/offlineReason'
import { useAppSelector } from '../store'
import { WaitGame } from '../components/laundry/WaitGame'

/**
 * The calm view over the content area while the connection is down: a neutral
 * spinner, a title and two muted lines, with no error colour. It hides every
 * panel's own fetch error behind one state (`offlineCover`).
 *
 * - `relay`: a relay Crew window retrying its dropped connection. A refusal,
 *   an ended session and the desktop app's "currently unavailable" notice are
 *   real failures and keep their own surfaces. Mounted for the whole life of
 *   the window, empty while connected, so its polite live region exists
 *   before the drop and announces it: a region does not speak the text it
 *   mounts with.
 *   When the Crew's owner turned Remote Control off it says so: the page
 *   still waits, and reconnects once the owner turns it back on.
 * - `host`: the owner's own dashboard on this machine with the gateway gone.
 *   No live region: the top bar's connection status already speaks the drop.
 *   Below its lines it offers `WaitGame` (shown after a while, or on Space);
 *   the game's own polite region speaks only game moves, never the outage.
 */
export function RelayReconnectingView({ cover }: { cover: OfflineCover }) {
  const { t } = useTranslation()
  const host = cover === 'host'
  const remoteOff = useAppSelector(s => s.dashboard.offlineCause === 'remote_off')
  const lines = host
    ? [t('app.host_offline_title'), t('app.host_offline_reconnects', { command: 'kirocrew gateway' }), t('app.host_offline_sessions_kept')]
    : remoteOff
      ? [t('app.relay_remote_control_off'), t('app.relay_remote_control_off_detail'), t('app.relay_remote_control_off_waits')]
      : [t('app.relay_crew_reconnecting'), t('app.relay_crew_dropped'), t('app.relay_crew_reconnects_on_its_own')]
  return (
    <div
      role={host ? undefined : 'status'}
      aria-live={host ? undefined : 'polite'}
      data-testid={host ? 'host-offline' : 'relay-reconnecting'}
      // Up, it is outside <main>, so a relay Crew tab's native pane would paint over it.
      data-relay-pane-overlay={cover ? '' : undefined}
      className={cover ? 'absolute inset-0 z-40 flex overflow-y-auto bg-bg px-6 text-center' : 'sr-only'}
    >
      {cover && (
        <div className="m-auto flex w-full max-w-4xl flex-col items-center gap-4 py-6">
          <div className="flex max-w-sm flex-none flex-col items-center gap-2">
            <Loader2 aria-hidden="true" size={20} strokeWidth={2} className="mb-1 text-muted animate-spin motion-reduce:animate-none" />
            <p className="text-base font-semibold text-text">{lines[0]}</p>
            <p className="text-sm text-muted">{lines[1]}</p>
            <p className="text-sm text-muted">{lines[2]}</p>
          </div>
          {host && <WaitGame className="h-[min(56vh,480px)] flex-none" />}
        </div>
      )}
    </div>
  )
}
