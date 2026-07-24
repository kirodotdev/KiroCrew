import { useIssueRadar } from '../context'
import { DASHBOARDS } from '../views/registry'

/** Body of the "Dashboards" accordion section: a nav list (Overview, Ranking,
 * Insights, Duplicates…) driven entirely by the registry. Selecting one opens
 * that dashboard in the main area. */
export default function DashboardsSection() {
  const { mainView, dashboardTab, openDashboard } = useIssueRadar()

  return (
    <div className="px-3 pt-1">
      <div className="flex flex-col gap-0.5">
        {DASHBOARDS.map((d) => {
          const isActive = mainView === 'dashboard' && dashboardTab === d.key
          return (
            <button
              key={d.key}
              onClick={() => openDashboard(d.key)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left cursor-pointer transition-colors ${
                isActive ? 'bg-accent-subtle text-text font-medium' : 'text-muted hover:bg-bg-hover'
              }`}
            >
              <d.icon size={14} className={`flex-shrink-0 ${isActive ? 'text-accent' : ''}`} />
              <span className="flex-1">{d.label}</span>
              {d.soon && (
                <span className="text-[10px] uppercase tracking-wide rounded px-1 py-0.5 bg-bg-hover text-muted whitespace-nowrap">coming soon</span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
