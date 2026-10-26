/**
 * The Kiro Agent settings shown in the Kiro Agent section of Customize >
 * Workflows (rendered there only while `agent.kas_workflows` is on and the
 * backend is Kiro Agent, i.e. behind `KasEngineGate`):
 *
 *   - Creator step effort: the gateway setting `agent.kas_creator_effort`, the
 *     effort of the creator step a prompt-created workflow starts with. Kiro
 *     Agent applies it per chat session, so it takes effect the next time a
 *     chat starts its session. Empty = Kiro Agent's own default.
 *   - Several workflows in one chat: stacked or combined. A device-local display
 *     preference, kept here beside the setting it belongs to rather than under
 *     Display, since the surface it shapes exists only while the switch is on.
 */
import { KAS_EFFORT_LEVELS } from '../../../api/client'
import { SettingsButtonGroup, SettingsSelect } from '../../../components/settings'
import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'
import { effortLabel } from './kasModel'
import { KAS_CREATOR_EFFORT_CONFIG_KEY, useKasWorkflowsEnabled, useKasCreatorEffort, useKasDockMode, type KasDockMode } from './useKasWorkflows'

export function KasCreatorEffortSetting() {
  const { enabled: available } = useKasWorkflowsEnabled()
  const { value, loaded, save } = useKasCreatorEffort(true)
  const options = ['', ...KAS_EFFORT_LEVELS]
  return (
    <div className="flex flex-col gap-1">
      <SettingsSelect
        label={i18nT('pages.developer.featurePreviewsTab.kas_creator_effort')}
        description={i18nT('pages.developer.featurePreviewsTab.kas_creator_effort_desc')}
        value={value}
        options={options}
        optionLabels={options.map(o => (o ? effortLabel(o) : i18nT('pages.developer.featurePreviewsTab.kas_creator_effort_default')))}
        onChange={v => save.mutate(v)}
        disabled={!available || !loaded || save.isPending}
        configKey={KAS_CREATOR_EFFORT_CONFIG_KEY}
      />
      {save.isError && <ErrorNotice message={i18nT('pages.developer.featurePreviewsTab.kas_creator_effort_failed')} />}
    </div>
  )
}

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
