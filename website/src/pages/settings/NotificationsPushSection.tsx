import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { usePushSubscription } from '../../hooks/usePushSubscription'
import { i18nT } from '../../i18n/t'

/**
 * Web Push subscription control, mounted by NotificationsPanel's Desktop alerts
 * page. A single gesture-driven toggle: clicking it runs the permission prompt +
 * PushManager.subscribe inside the user gesture (both require one). On an
 * unsupported browser or an iOS Safari tab (where the Push API needs a
 * home-screen install) it shows an explanatory hint instead of a toggle that
 * cannot work.
 *
 * Its own file so the settings registry maps it to the `alerts` rail page
 * (settingsExtract FILE map) instead of the bare Notifications tab.
 */
export function PushSection() {
  const push = usePushSubscription()
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
  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.push_section')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.notificationsPanel.push_toggle_label')}
          description={i18nT('pages.settings.notificationsPanel.push_toggle_description')}
          checked={push.subscribed}
          disabled={push.busy}
          onChange={v => { if (v) void push.subscribe(); else void push.unsubscribe() }}
        />
        {push.error && <ErrorNotice message={push.error} variant="inline" askAgent />}
      </SettingsCard>
    </SettingsSection>
  )
}
