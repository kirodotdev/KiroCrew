/**
 * RelayCrewsMenu: the desktop app's list of remote Crews reachable through the
 * Kiro relay, as a chip beside the instance switcher that opens a popover.
 *
 * Renders nothing unless the desktop bridge exists AND the gateway's crews route
 * answers (it 404s while `tunnel.relay_viewer_enabled` is off), so the default
 * header is unchanged. Open hands only the endpoint ID to the desktop shell,
 * which opens that Crew in its own window.
 *
 * shortcut: v1 opens each Crew in its own window. `RelayCrewList` takes the
 * entries and an `onOpen` callback, so moving it into Instances panes later
 * swaps this caller, not the list.
 */
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { RadioTower } from 'lucide-react'
import type { RelayCrewEntry } from '../api/client'
import {
  isRelayCrewOnline, openRelayCrew, relayCrewStatusText, relayRefusalText, useRelayViewerCrews,
} from '../hooks/useRelayViewerCrews'
import { Btn, PanelSectionHeader } from './ui'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import ErrorNotice from './ErrorNotice'

export function RelayCrewList({ crews, onOpen, refusals }: {
  crews: RelayCrewEntry[]
  onOpen: (endpointId: string) => void
  /** Plain refusal text per endpoint ID, from that Crew's last Open. */
  refusals: Record<string, string>
}) {
  const { t } = useTranslation()
  if (crews.length === 0) {
    return <div className="text-[12px] text-muted px-1 py-2">{t('components.relayCrews.empty')}</div>
  }
  return (
    <ul className="flex flex-col gap-1" aria-label={t('components.relayCrews.list_label')}>
      {crews.map(c => {
        const online = isRelayCrewOnline(c)
        const type = c.crewType.toUpperCase() === 'TEAM'
          ? t('components.relayCrews.type_team')
          : c.crewType.toUpperCase() === 'PERSONAL' ? t('components.relayCrews.type_personal') : c.crewType
        return (
          <li
            key={c.endpointId}
            data-testid="relay-crew-row"
            data-online={online ? 'true' : 'false'}
            className="flex flex-col gap-1 px-1 py-1"
          >
            {/* Only the dot and the text dim: the Open button shows its own disabled style. */}
            <div className="flex items-center gap-2 min-w-0">
              <span
                className={`w-2 h-2 rounded-full shrink-0 ${online ? 'bg-[var(--ok)]' : 'bg-[var(--muted)] opacity-50'}`}
                aria-hidden
              />
              <div className={`flex flex-col min-w-0 flex-1 ${online ? '' : 'opacity-50'}`} title={c.relayConnectionStatus}>
                <span className="text-[13px] text-text truncate">{c.name}</span>
                <span className="text-[11px] text-muted">
                  {type} · {relayCrewStatusText(c)}
                </span>
              </div>
              <Btn
                className="shrink-0"
                aria-label={t('components.relayCrews.open_crew', { name: c.name })}
                disabled={!online}
                title={online ? undefined : t('components.relayCrews.refusal_offline')}
                onClick={() => onOpen(c.endpointId)}
              >
                {t('components.relayCrews.open')}
              </Btn>
            </div>
            {refusals[c.endpointId] && (
              <ErrorNotice variant="inline" message={refusals[c.endpointId]} askAgent testId="relay-crew-refusal" />
            )}
          </li>
        )
      })}
    </ul>
  )
}

export default function RelayCrewsMenu() {
  const { t } = useTranslation()
  const { hidden, crews, refusal } = useRelayViewerCrews()
  const [refusals, setRefusals] = useState<Record<string, string>>({})
  const onOpen = useCallback((endpointId: string) => {
    setRefusals(prev => {
      const { [endpointId]: _dropped, ...rest } = prev
      return rest
    })
    void openRelayCrew(endpointId).then(res => {
      if (!res.ok) setRefusals(prev => ({ ...prev, [endpointId]: relayRefusalText(res.error) }))
    })
  }, [])
  if (hidden) return null
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Btn className="shrink-0" data-testid="relay-crews-trigger">
          <RadioTower size={14} aria-hidden />
          {t('components.relayCrews.title')}
        </Btn>
      </PopoverTrigger>
      <PopoverContent variant="list" align="start" className="w-80">
        <div className="flex flex-col gap-1">
          <PanelSectionHeader label={t('components.relayCrews.title')} className="px-1 pt-1" />
          {refusal
            ? <ErrorNotice variant="inline" message={refusal} askAgent testId="relay-crews-list-refusal" />
            : <RelayCrewList crews={crews} onOpen={onOpen} refusals={refusals} />}
        </div>
      </PopoverContent>
    </Popover>
  )
}
