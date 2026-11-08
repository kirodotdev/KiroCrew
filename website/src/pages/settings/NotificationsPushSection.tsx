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
 * The hint only points back at the System notifications row above, which
 * already carries the full "blocked" sentence; repeating it here read as two
 * separate problems (#23984).
 *
 * While a subscribe or unsubscribe call is in flight the toggle is disabled
 * and a one-line status under it says which way it is going. The hook only
 * sets `busy` inside those two calls and leaves `subscribed` untouched until
 * the call ends, so `busy && !subscribed` is "turning on" and
 * `busy && subscribed` is "turning off" with no extra state. While busy the
 * switch already shows the position it is going to, so the switch and the
 * line under it never disagree. The `role="status"` container stays mounted
 * and empty when idle, and only its text changes: a live region that appears
 * already holding its text is not announced by every screen reader.
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
          checked={push.busy ? !push.subscribed : push.subscribed}
          disabled={push.busy || blocked}
          describedBy={blocked ? blockedHintId : undefined}
          onChange={v => { if (v) void push.subscribe(); else void push.unsubscribe() }}
        />
        <div
          role="status"
          data-testid="push-busy-status"
          className={push.busy ? 'text-[12px] text-muted' : 'sr-only'}
        >
          {push.busy
            ? (push.subscribed
                ? i18nT('pages.settings.notificationsPanel.push_turning_off')
                : i18nT('pages.settings.notificationsPanel.push_turning_on'))
            : null}
        </div>
        {blocked && (
          <div id={blockedHintId} data-testid="push-blocked-hint" className="text-[12px] text-muted">
            {i18nT('pages.settings.notificationsPanel.push_blocked_hint')}
          </div>
        )}
        {push.error && <ErrorNotice message={push.error} variant="inline" askAgent />}
      </SettingsCard>
    </SettingsSection>
  )
}
