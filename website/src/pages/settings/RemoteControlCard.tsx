/**
 * Personal Remote Control: the notice and one-click off.
 *
 * Renders nothing unless config asks for Remote Control or a relay is running, so a
 * Crew that never turned it on shows no card. When the Kiro sign-in is refused (an
 * API key or an external identity provider) it says why, from the backend's
 * `reason` code, in this dashboard's own translated copy, and says the setting is
 * still on, so it is plain what the button does. "Turn off" writes an
 * explicit `false`, so Remote Control stays off across a restart; turning it back
 * on is the terminal command, which refuses the same sign-ins.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Trans } from 'react-i18next'
import { api, type RemoteControlState } from '../../api/client'
import { ApiError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

export const REMOTE_CONTROL_QUERY_KEY = ['remote-control'] as const
const TURN_ON_COMMAND = 'kirocrew remote-control on'

/** The command, verbatim and never translated, for the catalog's `<command/>` slot. */
function TurnOnCommand() {
  return <code className="text-text">{TURN_ON_COMMAND}</code>
}

/** The translated sentence for a refusal code, or `null` when nothing is refused. */
export function remoteControlReasonText(reason: RemoteControlState['reason']): string | null {
  switch (reason) {
    case 'api_key':
      return i18nT('pages.settings.remoteControlCard.reason_api_key')
    case 'external_idp':
      return i18nT('pages.settings.remoteControlCard.reason_external_idp')
    case 'not_signed_in':
      return i18nT('pages.settings.remoteControlCard.reason_not_signed_in')
    case 'unknown_sign_in':
      return i18nT('pages.settings.remoteControlCard.reason_unknown_sign_in')
    case 'locked':
      return i18nT('pages.settings.remoteControlCard.reason_locked')
    default:
      return null
  }
}

export function RemoteControlCard() {
  const queryClient = useQueryClient()
  const [turnedOff, setTurnedOff] = useState(false)
  const { data, error: loadError } = useQuery<RemoteControlState>({
    queryKey: REMOTE_CONTROL_QUERY_KEY,
    queryFn: () => api.remoteControl(),
    refetchInterval: 15_000,
  })
  const off = useMutation({
    mutationFn: () => api.setRemoteControl(false),
    onSuccess: (next) => {
      queryClient.setQueryData(REMOTE_CONTROL_QUERY_KEY, next)
      setTurnedOff(true)
    },
  })

  // A non-owner is refused by design (the route is owner-only), so that 403 is not a
  // failure to show. Any other read failure is, even while an earlier read is still
  // cached: that cached status may be stale, and hiding the error would hide that.
  const readFailed =
    Boolean(loadError) && !(loadError instanceof ApiError && loadError.status === 403)
  // No hand-off: the Remote Crew panel below this card can hold an unsaved add-crew
  // form, and opening a chat would unmount it.
  const readError = readFailed ? (
    <ErrorNotice
      title={i18nT('pages.settings.remoteControlCard.load_failed')}
      message={loadError instanceof Error ? loadError.message : String(loadError)}
    />
  ) : null

  const shown = data?.requested || data?.running
  if (!data || (!shown && !turnedOff)) {
    if (!readError) return null
    return (
      <SettingsSection title={i18nT('pages.settings.remoteControlCard.title')}>
        {readError}
      </SettingsSection>
    )
  }
  const refusal = remoteControlReasonText(data.reason)
  return (
    <SettingsSection title={i18nT('pages.settings.remoteControlCard.title')}>
      <SettingsCard>
        <div className="space-y-3" data-testid="remote-control-card">
          {readError}
          {shown && refusal && (
            <p className="m-0 text-[13px] text-warn" role="status">
              {i18nT('pages.settings.remoteControlCard.blocked')} {refusal}
            </p>
          )}
          {shown && !refusal && data.running && (
            <p className="m-0 text-[13px]">{i18nT('pages.settings.remoteControlCard.on')}</p>
          )}
          {shown && !refusal && !data.running && (
            <p className="m-0 text-[13px]" role="status">
              {i18nT('pages.settings.remoteControlCard.restart_to_connect')}
            </p>
          )}
          {!shown && (
            <p className="m-0 text-[13px]" role="status">
              {i18nT('pages.settings.remoteControlCard.off_saved')}{' '}
              <Trans
                i18nKey="pages.settings.remoteControlCard.turn_on_hint"
                components={{ command: <TurnOnCommand /> }}
              />
            </p>
          )}
          {/* No hand-off: same reason as the read failure above. */}
          {off.isError && (
            <ErrorNotice
              title={i18nT('pages.settings.remoteControlCard.turn_off_failed')}
              message={off.error instanceof Error ? off.error.message : String(off.error)}
            />
          )}
          {shown && (
            <Btn onClick={() => off.mutate()} disabled={off.isPending}>
              {off.isPending
                ? i18nT('pages.settings.remoteControlCard.turning_off')
                : refusal
                  ? i18nT('pages.settings.remoteControlCard.keep_off')
                  : i18nT('pages.settings.remoteControlCard.turn_off')}
            </Btn>
          )}
          {/* Says what the click costs: open relayed connections close, and undoing it
              needs the terminal (guard.stop_after_drain drains, then stops the relay). */}
          {shown && (
            <p className="m-0 text-[12px] text-muted" data-testid="remote-control-off-effect">
              {data.running && (
                <>{i18nT('pages.settings.remoteControlCard.turn_off_disconnects')} </>
              )}
              <Trans
                i18nKey="pages.settings.remoteControlCard.turn_off_needs_terminal"
                components={{ command: <TurnOnCommand /> }}
              />
            </p>
          )}
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
