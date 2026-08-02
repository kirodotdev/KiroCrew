import { ArrowRight, Import } from 'lucide-react'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Btn } from '../../components/ui'
import PortabilityTab from '../overview/PortabilityTab'

import { i18nT } from '../../i18n/t'
export function ImportPanel() {
  return (
    <>
      <SettingsSection title={i18nT('pages.settings.importPanel.agent_data')}>
        <SettingsCard>
          <div className="flex items-center justify-between gap-4 py-1.5">
            <div className="flex min-w-0 items-start gap-3">
              <Import className="lucide-inline mt-0.5 shrink-0 text-muted" />
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-text">
                  {i18nT('pages.settings.importPanel.import_from_another_agent')}
                </div>
                <div className="mt-0.5 text-[12px] text-muted">
                  {i18nT('pages.settings.importPanel.review_supported_sessions_memories_workspaces_mc')}
                </div>
              </div>
            </div>
            <Btn
              type="button"
              className="shrink-0"
              onClick={() => window.dispatchEvent(new Event('mc-start-import'))}
            >
              {i18nT('pages.settings.importPanel.import_from_another_agent')}
              <ArrowRight className="lucide-inline" />
            </Btn>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* Configuration backup — this tab is the one home for getting data in
          and out. */}
      <SettingsSection title={i18nT('pages.settings.importPanel.back_up_restore_configuration')}>
        <PortabilityTab />
      </SettingsSection>
    </>
  )
}
