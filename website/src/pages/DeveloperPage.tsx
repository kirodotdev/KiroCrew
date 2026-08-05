import { lazy, Suspense } from 'react'
import { ScrollText, Monitor, Brain, Archive, Database, Network, Activity, FileCode2 } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import { ContentSkeleton } from '../components/ui'
import { LogViewer } from './LogsPage'
import SystemPage from './SystemPage'
import TelemetryPanel from './TelemetryPanel'
import SessionArchive from './SessionArchive'
import LocalStorageDebug from './LocalStorageDebug'
import { SharedMcpGatewayToggle } from './settings/SharedMcpGatewayToggle'
import { McpPoolableServers } from './settings/McpPoolableServers'
import { KiroCrewCfgTab, AgentCfgTab } from './overview'

/**
 * Lazy: MemoryGraphTab is the only eager owner of the sigma/graphology stack
 * (vendor-graph, ~180 KB gzip), which a static import keeps in the entry
 * modulepreload set for every page load even though this tab is one of eight on
 * an internals-only route. Deferred behind `lazy()`, the chunk is fetched when
 * the Memory tab is first opened.
 */
const MemoryGraphTab = lazy(() => import('./overview/MemoryGraphTab'))

import { i18nT } from '../i18n/t'

/**
 * A FUNCTION, not a module-level array: the labels and descriptions are
 * translated, and a module-level constant is evaluated once at import — which
 * would freeze whichever language was active at boot and leave the tab rail
 * English after a language switch. Called once per render instead, mirroring
 * `buildTabs()` in SettingsPage.tsx, which feeds the same SidePanelLayout.
 */
function buildTabs() {
  return [
    { key: 'logs', label: i18nT('pages.developerPage.tabs.logs.label'), icon: <ScrollText size={16} />, description: i18nT('pages.developerPage.tabs.logs.description') },
    { key: 'system', label: i18nT('pages.developerPage.tabs.system.label'), icon: <Monitor size={16} />, description: i18nT('pages.developerPage.tabs.system.description') },
    { key: 'telemetry', label: i18nT('pages.developerPage.tabs.telemetry.label'), icon: <Activity size={16} />, description: i18nT('pages.developerPage.tabs.telemetry.description') },
    { key: 'storage', label: i18nT('pages.developerPage.tabs.storage.label'), icon: <Database size={16} />, description: i18nT('pages.developerPage.tabs.storage.description') },
    { key: 'mcp-pool', label: i18nT('pages.developerPage.tabs.mcpPool.label'), icon: <Network size={16} />, description: i18nT('pages.developerPage.tabs.mcpPool.description') },
    { key: 'memory', label: i18nT('pages.developerPage.tabs.memory.label'), icon: <Brain size={16} />, description: i18nT('pages.developerPage.tabs.memory.description') },
    { key: 'config', label: i18nT('pages.developerPage.tabs.config.label'), icon: <FileCode2 size={16} />, description: i18nT('pages.developerPage.tabs.config.description') },
    { key: 'archive', label: i18nT('pages.developerPage.tabs.archive.label'), icon: <Archive size={16} />, description: i18nT('pages.developerPage.tabs.archive.description') },
  ]
}

export default function DeveloperPage() {
  const tabs = buildTabs()
  return (
    <SidePanelLayout title={i18nT('pages.developerPage.developer')} tabs={tabs} rememberKey="developer">
      {tab => <>
        {tab === 'logs' && <div className="h-[calc(100vh-160px)] min-h-[300px] flex flex-col overflow-hidden"><LogViewer compact /></div>}
        {tab === 'system' && <SystemPage embedded />}
        {tab === 'telemetry' && <TelemetryPanel />}
        {tab === 'storage' && <LocalStorageDebug />}
        {tab === 'mcp-pool' && (
          <>
            <SharedMcpGatewayToggle />
            <McpPoolableServers />
          </>
        )}
        {tab === 'memory' && (
          <>
            {/* The memory GRAPH visualizer is an internals view. The
                user-facing memory browser (settings, preferences, projects,
                history, lessons + vector store card) lives in Settings >
                Overview > Memory. */}
            <Suspense fallback={<ContentSkeleton rows={6} />}>
              <MemoryGraphTab />
            </Suspense>
          </>
        )}
        {tab === 'config' && (
          <>
            <KiroCrewCfgTab />
            <AgentCfgTab />
          </>
        )}
        {tab === 'archive' && <SessionArchive />}
      </>}
    </SidePanelLayout>
  )
}
