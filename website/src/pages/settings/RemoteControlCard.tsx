/**
 * Personal Remote Control: whether it is on, and one-click off.
 *
 * Renders nothing unless config asks for Remote Control or a relay is running, so a
 * Crew that never turned it on shows no card. On a build that locks the tunnel it
 * says the build manages Remote Control instead of asking for a restart that would
 * never connect. "Turn off" writes an explicit
 * `false`, so Remote Control stays off across a restart; turning it back on is the
 * terminal command.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Trans } from 'react-i18next'
import { api, type RemoteControlState } from '../../api/client'
import { ApiError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

export const REMOTE_CONTROL_QUERY_KEY = ['remote-control'] as const
const TURN_ON_COMMAND = 'kirocrew remote-control on'

/** The command, verbatim and never translated, with a copy button, for the catalog's `<command/>` slot. */
function TurnOnCommand() {
  return (
    <span className="inline-flex items-center gap-1 align-middle">
      <code className="text-text">{TURN_ON_COMMAND}</code>
      <CopyCommandButton text={TURN_ON_COMMAND} />
    </span>
  )
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
    mutationFn: () => api.turnOffRemoteControl(),
    onSuccess: async (next) => {
      // A poll still in flight read the setting before the off landed; cancel it so
      // its late answer cannot overwrite this one.
      await queryClient.cancelQueries({ queryKey: REMOTE_CONTROL_QUERY_KEY })
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
  return (
    <SettingsSection title={i18nT('pages.settings.remoteControlCard.title')}>
      <SettingsCard>
        <div className="space-y-3" data-testid="remote-control-card">
          {readError}
          {/* This build ignores the setting, so a restart would never connect. */}
          {shown && data.locked && (
            <p className="m-0 text-[13px]" role="status">
              {i18nT('pages.settings.remoteControlCard.locked')}
            </p>
          )}
          {shown && !data.locked && data.running && (
            <p className="m-0 text-[13px]">{i18nT('pages.settings.remoteControlCard.on')}</p>
          )}
          {shown && !data.locked && !data.running && (
            <p className="m-0 text-[13px]" role="status">
              {i18nT('pages.settings.remoteControlCard.restart_to_connect')}
            </p>
          )}
          {!shown && (
            <p className="m-0 text-[13px]" role="status">
              {i18nT('pages.settings.remoteControlCard.off_saved')}{' '}
              {/* Locked: `remote-control on` is refused here, so no turn-on hint. */}
              {data.locked ? (
                i18nT('pages.settings.remoteControlCard.locked')
              ) : (
                <Trans
                  i18nKey="pages.settings.remoteControlCard.turn_on_hint"
                  components={{ command: <TurnOnCommand /> }}
                />
              )}
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
                : i18nT('pages.settings.remoteControlCard.turn_off')}
            </Btn>
          )}
          {/* Says what the click costs: open relayed connections close, and undoing it
              needs the terminal (guard.stop_after_drain drains, then stops the relay). */}
          {/* Locked: `remote-control on` is refused here, so no turn-on hint. */}
          {shown && !data.locked && (
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
