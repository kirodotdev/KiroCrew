import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'
import { useUpdateSwitchesShown } from '../utils/updateSwitches'
import { AppAutoDownloadSwitch, GatewayAutoUpdateSwitch } from '../pages/settings/AboutPanel'

/**
 * The update controls at the foot of the post-update "What's new" modal: the
 * same rows Settings → About draws, under the same rule
 * (`utils/updateSwitches`), so the two surfaces cannot offer different
 * switches for one window. Drawn without highlight anchors, so a Settings deep
 * link can only land on About's rows, and reading the saved gateway value
 * each time the modal opens.
 *
 * Where an operator's update policy owns the gateway's updates, that is said
 * above the rows: the gateway switch still governs whether the policy's apply
 * command runs unattended.
 */
export default function WhatsNewAutoUpdateToggle({ onHandoff }: { onHandoff: () => void }) {
  const managedByPolicy = useAppSelector(s => s.dashboard.status?.update_managed_by) === 'command'
  const shown = useUpdateSwitchesShown()
  if (!shown.app && !shown.gateway && !managedByPolicy) return null
  return (
    <div className="mt-4" data-testid="whats-new-update-switches">
      {managedByPolicy && (
        <p className="text-[13px] text-muted pt-3 border-t border-border">{i18nT('pages.settings.aboutPanel.updates_managed_by_policy')}</p>
      )}
      {shown.app && (
        <AppAutoDownloadSwitch anchor={false} onHandoff={onHandoff}
          unreadable={i18nT('pages.settings.aboutPanel.auto_download_unreadable')} />
      )}
      {shown.gateway && <GatewayAutoUpdateSwitch anchor={false} readOnMount onHandoff={onHandoff} />}
    </div>
  )
}
