import { LayoutDashboard, CircleDot, Settings, Radar } from 'lucide-react'
import { useIssueRadar } from '../context'
import { APP_VERSION } from '../lib/format'
import AccordionSection from './Accordion'
import DashboardsSection from './DashboardsSection'
import FiltersSection from './FiltersSection'
import SettingsSection from './SettingsSection'
import RepoSwitcher from './RepoSwitcher'

/** The left rail: a prominent repo switcher pinned at the top, then a
 * three-section accordion (Dashboards / Issues / Settings) that follows the
 * main view (see context follow-mode), with the app identity at the very
 * bottom. Clicking a section header navigates to that section's default page
 * (not just expand it), so you never stay on the previous view. */
export default function LeftRail() {
  const { expanded, openDashboard, openIssues, openSettings } = useIssueRadar()

  return (
    <aside className="w-72 flex-shrink-0 flex flex-col min-h-0 py-2 gap-2">
      {/* Repo switcher — top of the rail, opens downward. */}
      <div className="px-2">
        <RepoSwitcher />
      </div>

      <AccordionSection
        title="Dashboards"
        icon={LayoutDashboard}
        expanded={expanded === 'dashboards'}
        onToggle={() => openDashboard('overview')}
      >
        <DashboardsSection />
      </AccordionSection>

      <AccordionSection
        title="Issues"
        icon={CircleDot}
        expanded={expanded === 'filters'}
        onToggle={() => openIssues()}
      >
        <FiltersSection />
      </AccordionSection>

      <AccordionSection
        title="Settings"
        icon={Settings}
        expanded={expanded === 'settings'}
        onToggle={() => openSettings()}
      >
        <SettingsSection />
      </AccordionSection>

      {/* App identity — bottom-most. */}
      <div className="px-3 pb-2 flex items-center gap-2">
        <Radar size={16} className="text-accent flex-shrink-0" />
        <span className="text-[14px] font-medium text-text">Issue Radar</span>
        <span className="ml-auto text-[12px] text-muted opacity-70">v{APP_VERSION}</span>
      </div>
    </aside>
  )
}
