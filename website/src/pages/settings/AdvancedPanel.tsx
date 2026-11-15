import { AllSettingsList } from '../../components/allSettings/AllSettingsList'
import { i18nT } from '../../i18n/t'

/**
 * Settings > Advanced — every config key, so a setting with no control of its
 * own can be changed without editing config.json. The rows are drawn from the
 * gateway's schema by `AllSettingsList`; this panel only frames them, and the
 * `data-setting-label` anchor is what the page's search entry lands on. No
 * second heading: the tab's own title already names the page.
 */
export function AdvancedPanel() {
  return (
    <div data-setting-label={i18nT('settings.advanced.title')} className="mt-4">
      <AllSettingsList />
    </div>
  )
}
