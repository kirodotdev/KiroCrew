import { useId } from 'react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { usePushSubscription } from '../../hooks/usePushSubscription'
import { useNotificationPermission } from '../../hooks/useNotificationPermission'
import { i18nT } from '../../i18n/t'

/**
 * Web Push subscription control, mounted by NotificationsPanel's Desktop alerts
 * page. A single gesture-driven toggle: clicking it runs the permission prompt +
 * PushManager.subscribe inside the user gesture (both require one). On an
 * unsupported browser or an iOS Safari tab (where the Push API needs a
 * home-screen install) it shows an explanatory hint instead of a toggle that
 * cannot work. While the browser has blocked notifications for the site the
 * toggle is disabled and the blocked hint sits beside it as its description:
 * no click here can succeed until the user changes the browser's site setting.
 *
 * Its own file so the settings registry maps it to the `alerts` rail page
 * (settingsExtract FILE map) instead of the bare Notifications tab.
 */
export function PushSection() {
  const push = usePushSubscription()
  const { permission } = useNotificationPermission()
  const blockedHintId = useId()
  if (!push.supported) {
    const hint = push.needsInstall
      ? i18nT('pages.settings.notificationsPanel.push_install_hint')
      : i18nT('pages.settings.notificationsPanel.push_unsupported')
    return (
      <SettingsSection title={i18nT('pages.settings.notificationsPanel.push_section')}>
        <SettingsCard>
          <div className="text-[12px] text-muted">{hint}</div>
        </SettingsCard>
      </SettingsSection>
    )
  }
  const blocked = permission === 'denied'
  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.push_section')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.notificationsPanel.push_toggle_label')}
          description={i18nT('pages.settings.notificationsPanel.push_toggle_description')}
          checked={push.subscribed}
          disabled={push.busy || blocked}
          describedBy={blocked ? blockedHintId : undefined}
          onChange={v => { if (v) void push.subscribe(); else void push.unsubscribe() }}
        />
        {blocked && (
          <div id={blockedHintId} data-testid="push-blocked-hint" className="text-[12px] text-muted">
            {i18nT('pages.settings.notificationsPanel.system_notifications_blocked')}
          </div>
        )}
        {push.error && <ErrorNotice message={push.error} variant="inline" askAgent />}
      </SettingsCard>
    </SettingsSection>
  )
}
