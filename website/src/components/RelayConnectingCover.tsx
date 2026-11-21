/**
 * What fills <main> while a relay Crew opens: its name and "Connecting…".
 * RelayCrewPaneHost shows it; App.tsx mounts that host in a box that is exactly
 * <main>, so a docked terminal beside it stays usable.
 */
import { Loader2 } from 'lucide-react'
import { i18nT } from '../i18n/t'

export default function RelayConnectingCover({ name }: { name: string }) {
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-bg" role="status" data-testid="relay-crew-connecting">
      <div className="flex flex-col items-center gap-3 text-center max-w-md">
        <Loader2 size={28} className="animate-spin text-muted" aria-hidden />
        <div className="text-sm font-medium text-text">{name}</div>
        <div className="text-[12px] text-muted">{i18nT('components.relayCrews.connecting')}</div>
      </div>
    </div>
  )
}
