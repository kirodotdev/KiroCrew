/**
 * A channel row's bridge route: which chat apps its notes are also sent to
 * ("Deliver to…") and the lowest priority that goes (the floor).
 *
 * The backend stores this as `deliver_to` / `deliver_min_priority` on the
 * channel's settings and validates it against the KNOWN transport ids, so a
 * route survives a chat app that is down for a while. The row therefore offers
 * only the apps that are connected right now AND that the bridge has a sink
 * for (`bridge_transports` on the channel list), plus any already in the route:
 * a picked app that has since disconnected, or that the bridge cannot deliver
 * to, stays checked and reads "unavailable", rather than vanishing from the row
 * while the backend still holds it.
 */
import { useContext, useId, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '../../components/ui/select'
import type { StatusData } from '../../types'

import { i18nT } from '../../i18n/t'

/** Transport ids a route may name, in the order the row lists them. Mirrors
 *  `BRIDGE_TRANSPORTS` in `notifications/bridge.py`; the label is the same
 *  catalog key System > Services uses for the channel. */
export const BRIDGE_TRANSPORT_LABEL_KEY: Record<string, string> = {
  slack: 'pages.servicesTab.slack',
  discord: 'pages.servicesTab.discord',
  telegram: 'pages.servicesTab.telegram',
  webex: 'pages.servicesTab.webex',
  wecom: 'pages.servicesTab.wecom',
}
const BRIDGE_TRANSPORTS = Object.keys(BRIDGE_TRANSPORT_LABEL_KEY)

/** Floors, tightest first, mirroring `DELIVER_MIN_PRIORITIES`. A route saved
 *  without one gets `critical` (`DEFAULT_DELIVER_MIN_PRIORITY`). */
const FLOOR_LABEL_KEY: Record<string, string> = {
  critical: 'pages.settings.notificationsPanel.deliver_floor_critical',
  default: 'pages.settings.notificationsPanel.deliver_floor_default',
  all: 'pages.settings.notificationsPanel.deliver_floor_all',
}
const DEFAULT_FLOOR = 'critical'

export type BridgePatch = { deliver_to?: string[]; deliver_min_priority?: string }

const noopSubscribe = () => () => {}

/**
 * The bridge transports the gateway reports connected, as a comma-joined
 * string so the store subscription compares by value.
 *
 * Read from `dashboard.status.channels`, the same live map System > Services
 * renders; Slack falls back to `slack_connected` for an older gateway that
 * sends no map. Tolerates a host with no store (an embedded panel, a test),
 * where nothing reads as connected.
 */
function useConnectedTransportSig(): string {
  const redux = useContext(ReactReduxContext)
  const subscribe = redux ? redux.store.subscribe : noopSubscribe
  return useSyncExternalStore(subscribe, () => {
    const status = (redux?.store.getState() as { dashboard?: { status?: StatusData | null } } | undefined)?.dashboard?.status
    return BRIDGE_TRANSPORTS.filter(t => {
      const live = status?.channels?.[t]?.connected
      return Boolean(t === 'slack' ? live ?? status?.slack_connected : live)
    }).join(',')
  })
}

export function DeliverToControl({ channel, name, deliverTo, floor, deliverable, saving = false, patch }: {
  channel: string
  /** The channel's visible label, for the controls' accessible names. */
  name: string
  deliverTo: string[] | undefined
  floor: string | undefined
  /** Transports with a bridge sink, from the channel list. */
  deliverable: string[] | undefined
  /** A route save for this channel is in flight: hold new picks until it settles.
   *  `aria-disabled`, not `disabled`, so the control just used keeps focus. */
  saving?: boolean
  patch: (channel: string, settings: BridgePatch) => void
}) {
  const sig = useConnectedTransportSig()
  const idBase = useId()
  const connected = new Set(sig ? sig.split(',') : [])
  // The stored settings are read leniently, so a hand-edited file can hold a
  // non-list here; it reads as no route, as the backend reads it.
  const deliverableList = Array.isArray(deliverable) ? deliverable : []
  const usable = (t: string) => connected.has(t) && deliverableList.includes(t)
  const picked = (Array.isArray(deliverTo) ? deliverTo : []).filter(t => typeof t === 'string' && Object.hasOwn(BRIDGE_TRANSPORT_LABEL_KEY, t))
  const offered = BRIDGE_TRANSPORTS.filter(t => usable(t) || picked.includes(t))
  if (offered.length === 0) return null

  const toggle = (transport: string, on: boolean) => {
    if (saving) return
    const next = BRIDGE_TRANSPORTS.filter(t => (t === transport ? on : picked.includes(t)))
    patch(channel, { deliver_to: next })
  }

  return (
    <div className="w-full flex flex-wrap items-center gap-x-3 gap-y-1.5 pl-0.5">
      <fieldset aria-busy={saving || undefined} className={`flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0 border-0 p-0 m-0 ${saving ? 'opacity-60' : ''}`}>
        <legend className="sr-only">{i18nT('pages.settings.notificationsPanel.deliver_to_group', { name })}</legend>
        <span aria-hidden="true" className="text-[11px] text-muted">{i18nT('pages.settings.notificationsPanel.deliver_to')}</span>
        {offered.map(t => {
          const label = i18nT(BRIDGE_TRANSPORT_LABEL_KEY[t])
          const down = !usable(t)
          const textId = `${idBase}-${t}`
          return (
            <label key={t} className={`inline-flex items-center gap-1.5 text-[12px] cursor-pointer ${down ? 'text-muted' : 'text-text'}`}>
              <input
                type="checkbox"
                className="h-4 w-4 accent-accent"
                aria-labelledby={textId}
                aria-disabled={saving || undefined}
                checked={picked.includes(t)}
                onChange={e => toggle(t, e.target.checked)}
              />
              <span id={textId}>
                {label}
                {down && <span className="text-[11px] italic"> ({i18nT('pages.settings.notificationsPanel.transport_unavailable')})</span>}
              </span>
            </label>
          )
        })}
      </fieldset>
      {picked.length > 0 && (
        <div className="shrink-0 w-full sm:w-48">
          <Select
            value={floor && Object.hasOwn(FLOOR_LABEL_KEY, floor) ? floor : DEFAULT_FLOOR}
            onValueChange={v => { if (!saving) patch(channel, { deliver_min_priority: v }) }}
          >
            <SelectTrigger aria-disabled={saving || undefined} aria-label={i18nT('pages.settings.notificationsPanel.deliver_min_priority_for', { name })}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.keys(FLOOR_LABEL_KEY).map(opt => (
                <SelectItem key={opt} value={opt}>{i18nT(FLOOR_LABEL_KEY[opt])}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  )
}
