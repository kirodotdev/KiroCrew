import { ArrowRight, Import } from 'lucide-react'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Btn } from '../../components/ui'
import PortabilityTab from '../overview/PortabilityTab'

export function ImportPanel() {
  return (
    <>
      <SettingsSection title="Agent data">
        <SettingsCard>
          <div className="flex items-center justify-between gap-4 py-1.5">
            <div className="flex min-w-0 items-start gap-3">
              <Import className="lucide-inline mt-0.5 shrink-0 text-muted" />
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-text">
                  Import from another agent
                </div>
                <div className="mt-0.5 text-[12px] text-muted">
                  Review supported sessions, memories, workspaces, MCP servers, skills,
                  schedules, and compatible settings.
                </div>
              </div>
            </div>
            <Btn
              type="button"
              className="shrink-0"
              onClick={() => window.dispatchEvent(new Event('mc-start-import'))}
            >
              Import from another agent
              <ArrowRight className="lucide-inline" />
            </Btn>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* Configuration backup (moved from Settings > Overview > Import/Export
          — this tab is the one home for getting data in and out). */}
      <SettingsSection title="Back up & restore configuration">
        <PortabilityTab />
      </SettingsSection>
    </>
  )
}
