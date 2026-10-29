import { lazy, Suspense, type ReactNode } from 'react'
import { ExternalLink, FlaskConical, ScrollText, Monitor, Brain, Archive, Database, Network, Activity, FileCode2, Bug } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { SettingsSubNav, type SubNavItem } from '../../components/SettingsSubNav'
import { ContentSkeleton } from '../../components/ui'
import { useLocalGateway } from '../../hooks/useLocalGateway'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'
import { CrewmatesSection } from './CrewmatesSection'
import AutomaticCardSetting from '../chat/command-center/AutomaticCardSetting'
import { i18nT } from '../../i18n/t'

/**
 * Lazy: every internals page loads its chunk when it is first opened. They sit
 * in the Settings chunk otherwise, which every Settings visit downloads, and
 * MemoryGraphTab alone carries the sigma/graphology stack (vendor-graph,
 * ~180 KB gzip) that a static import would put in the entry modulepreload set.
 */
const LogViewer = lazy(() => import('../LogsPage').then(m => ({ default: m.LogViewer })))
const SystemPage = lazy(() => import('../SystemPage'))
const TelemetryPanel = lazy(() => import('../TelemetryPanel'))
const SessionArchive = lazy(() => import('../SessionArchive'))
const LocalStorageDebug = lazy(() => import('../LocalStorageDebug'))
const McpManagement = lazy(() => import('./McpManagement').then(m => ({ default: m.McpManagement })))
const ConfigPage = lazy(() => import('../overview').then(m => ({
  default: () => <><m.KiroCrewCfgTab /><m.AgentCfgTab /></>,
})))
const DebugToolsTab = lazy(() => import('../developer/DebugToolsTab').then(m => ({ default: m.DebugToolsTab })))
const MemoryGraphTab = lazy(() => import('../overview/MemoryGraphTab'))

/** One lazy page under its loading skeleton. */
const loading = (page: ReactNode) => (
  <Suspense fallback={<ContentSkeleton rows={6} />}>{page}</Suspense>
)

/** The remote guide's SSH-tunnel section: it works on every desktop OS and
 *  covers both a hand-run forward and the app-held one (#3810). */
const LOCAL_GATEWAY_TUNNEL_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md#ssh-tunnel-laptop'

/** Settings > Developer tab: a SettingsSubNav whose first page, Feature
 *  Previews, holds the per-device opt-in switches, and whose other pages are
 *  the internals views (logs, system metrics, telemetry, storage, MCP pool,
 *  memory graph, config, debug tools, session archive). Every page is always
 *  listed: there is no consent gate in front of the internals views.
 *
 *  The Gateway section is desktop-app-only and appears only when the Electron
 *  bridge is present: a browser tab has no local gateway to start or stop. */
export function DeveloperPanel({ basePath }: { basePath?: string } = {}) {
  const { localGatewayEnabled, localGatewaySupported, setLocalGatewayEnabled } = useLocalGateway()

  const railItems: SubNavItem[] = [
    { key: 'feature-previews', label: i18nT('pages.settings.developerPanel.feature_previews'), icon: <FlaskConical size={16} /> },
    { key: 'logs', label: i18nT('pages.developerPage.tabs.logs.label'), icon: <ScrollText size={16} /> },
    { key: 'system', label: i18nT('pages.developerPage.tabs.system.label'), icon: <Monitor size={16} /> },
    { key: 'telemetry', label: i18nT('pages.developerPage.tabs.telemetry.label'), icon: <Activity size={16} /> },
    { key: 'storage', label: i18nT('pages.developerPage.tabs.storage.label'), icon: <Database size={16} /> },
    { key: 'mcp-pool', label: i18nT('pages.developerPage.tabs.mcpPool.label'), icon: <Network size={16} /> },
    { key: 'memory', label: i18nT('pages.developerPage.tabs.memory.label'), icon: <Brain size={16} /> },
    { key: 'config', label: i18nT('pages.developerPage.tabs.config.label'), icon: <FileCode2 size={16} /> },
    { key: 'debug-tools', label: i18nT('pages.developerPage.tabs.debugTools.label'), icon: <Bug size={16} /> },
    { key: 'archive', label: i18nT('pages.developerPage.tabs.archive.label'), icon: <Archive size={16} /> },
  ]

  return (
    <SettingsSubNav
      items={railItems}
      basePath={basePath}
      guideTargetPrefix="settings.sub.developer."
      railWidth={220}
      listLabel={i18nT('settings.tabs.developer.label')}
    >
      {active => {
        switch (active) {

        case 'feature-previews':
          return (
            <>
              {/* Automatic cards for every session: a gateway-wide spend
                  opt-in, off by default. Its own card because it is a server
                  setting, not a per-device switch. */}
              <SettingsSection title={i18nT('pages.settings.developerPanel.developer_tools')}>
                <SettingsCard>
                  <div data-testid="developer-automatic-cards">
                    <AutomaticCardSetting active />
                  </div>
                </SettingsCard>
              </SettingsSection>
              <FeaturePreviewsSection />
              {/* The crewmate feature switches (`CrewmatesSection.tsx`). */}
              <CrewmatesSection />
              {localGatewaySupported && (
                <SettingsSection title={i18nT('pages.settings.developerPanel.gateway')}>
                  <SettingsCard>
                    <SettingsToggle
                      label={i18nT('pages.settings.developerPanel.run_a_local_gateway')}
                      description={i18nT('pages.settings.developerPanel.turn_it_off_only_when_a_gateway_already_answers')}
                      checked={localGatewayEnabled}
                      onChange={setLocalGatewayEnabled}
                    />
                    {/* "Off" has a prerequisite: this app's port must reach a
                        crew saved for it (Set Remote Host…), through the
                        app-held tunnel or one the user runs. A hand-run tunnel
                        with no saved crew is refused as a foreign holder
                        (#3810). The guide's SSH-tunnel section covers it. */}
                    <a
                      href={LOCAL_GATEWAY_TUNNEL_GUIDE}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline py-1"
                    >
                      {i18nT('pages.settings.developerPanel.how_to_set_up_an_ssh_tunnel')}
                      <ExternalLink size={13} className="lucide-inline" />
                    </a>
                  </SettingsCard>
                </SettingsSection>
              )}
            </>
          )

        case 'logs':
          return <div className="h-[calc(100vh-160px)] min-h-[300px] flex flex-col overflow-hidden">{loading(<LogViewer compact />)}</div>

        case 'system':
          return loading(<SystemPage embedded />)

        case 'telemetry':
          return loading(<TelemetryPanel />)

        case 'storage':
          return loading(<LocalStorageDebug />)

        case 'mcp-pool':
          return loading(<McpManagement />)

        case 'memory':
          // The memory GRAPH visualizer is an internals view. The user-facing
          // memory browser lives in Settings > Overview > Memory.
          return loading(<MemoryGraphTab />)

        case 'config':
          return loading(<ConfigPage />)

        case 'debug-tools':
          return loading(<DebugToolsTab />)

        case 'archive':
          return loading(<SessionArchive />)

        default:
          return null
        }
      }}
    </SettingsSubNav>
  )
}
