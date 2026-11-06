/**
 * The Kiro Agent setting shown in the Kiro Agent section of Customize >
 * Workflows (rendered there only while `agent.kas_workflows` is on and the
 * backend is Kiro Agent, i.e. behind `KasEngineGate`): several workflows in one
 * chat, stacked or combined. A device-local display preference, kept here
 * rather than under Display, since the surface it shapes exists only while the
 * switch is on.
 */
import { SettingsButtonGroup } from '../../../components/settings'
import { i18nT } from '../../../i18n/t'
import { useKasDockMode, type KasDockMode } from './useKasWorkflows'

export function KasDockModeSetting() {
  const [mode, setMode] = useKasDockMode()
  return (
    <SettingsButtonGroup
      label={i18nT('pages.developer.featurePreviewsTab.kas_dock_mode')}
      description={i18nT('pages.developer.featurePreviewsTab.kas_dock_mode_desc')}
      value={mode}
      options={[
        { value: 'stack', label: i18nT('pages.developer.featurePreviewsTab.kas_dock_stack') },
        { value: 'combined', label: i18nT('pages.developer.featurePreviewsTab.kas_dock_combined') },
      ]}
      onChange={v => setMode(v as KasDockMode)}
    />
  )
}
