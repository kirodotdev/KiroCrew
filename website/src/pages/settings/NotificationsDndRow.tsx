/**
 * Settings > Notifications > Desktop alerts > Do not disturb: one select that
 * starts or ends the gateway's timed Do Not Disturb. Saved to the gateway, so
 * it applies to every device and to chat app deliveries, unlike the Sound
 * pane's per-browser "Mute sounds".
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSelect } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtTime } from '../../i18n/format'
import { NOTIFICATION_DND_KEY, useNotificationDnd, type NotificationDnd } from '../../hooks/useNotificationDnd'
import { MUTE_CHOICES, MUTED_NOW, muteEnd, muteLabel, type MuteChoice } from './muteChoices'

/** The select's only value while the setting cannot be read: never a guessed Off. */
const UNKNOWN = 'unknown'

export function DoNotDisturbRow() {
  const qc = useQueryClient()
  const { endMs, loadFailed } = useNotificationDnd()
  const save = useMutation({
    mutationFn: (untilMs: number | null) => api.setNotificationDnd(untilMs === null ? null : untilMs / 1000),
    onSuccess: (data: NotificationDnd) => {
      void qc.cancelQueries({ queryKey: NOTIFICATION_DND_KEY })
      qc.setQueryData(NOTIFICATION_DND_KEY, data)
    },
  })
  const now = Date.now()
  const on = endMs !== null
  return (
    <>
      {/* Disabled when the read failed: "Off" would be a guess, not the gateway's state. */}
      <SettingsSelect
        label={i18nT('pages.settings.notificationsPanel.do_not_disturb')}
        hint={i18nT('pages.settings.notificationsPanel.do_not_disturb_description')}
        value={loadFailed ? UNKNOWN : on ? MUTED_NOW : 'off'}
        options={loadFailed ? [UNKNOWN] : [...(on ? [MUTED_NOW] : []), ...MUTE_CHOICES]}
        optionLabels={loadFailed ? [i18nT('pages.settings.notificationsPanel.dnd_state_unknown')] : [
          ...(on ? [i18nT('pages.settings.notificationsPanel.dnd_on_until', { time: fmtTime(endMs) })] : []),
          ...MUTE_CHOICES.map(c => muteLabel(c, now)),
        ]}
        onChange={v => {
          if (v === MUTED_NOW || v === UNKNOWN) return
          save.mutate(v === 'off' ? null : muteEnd(v as Exclude<MuteChoice, 'off'>, Date.now()))
        }}
        busy={save.isPending}
        disabled={loadFailed}
      />
      {/* askAgent on for both notices: the select holds no draft, and a failed
          read or save leaves the gateway's state as it was, so leaving loses nothing. */}
      {loadFailed && (
        <ErrorNotice
          className="mt-1"
          message={i18nT('pages.settings.notificationsPanel.dnd_load_failed')}
          askAgent
        />
      )}
      {save.isError && (
        <ErrorNotice
          className="mt-1"
          message={i18nT('pages.settings.notificationsPanel.dnd_save_failed')}
          onDismiss={() => save.reset()}
          askAgent
        />
      )}
    </>
  )
}
