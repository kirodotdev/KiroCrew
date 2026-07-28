import { ScrollText, Monitor, Brain, Archive, Database, Network, Activity, FileCode2 } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import { LogViewer } from './LogsPage'
import SystemPage from './SystemPage'
import TelemetryPanel from './TelemetryPanel'
import SessionArchive from './SessionArchive'
import LocalStorageDebug from './LocalStorageDebug'
import { SharedMcpGatewayToggle } from './settings/SharedMcpGatewayToggle'
import { McpPoolableServers } from './settings/McpPoolableServers'
import { KiroCrewCfgTab, AgentCfgTab } from './overview'
import MemoryGraphTab from './overview/MemoryGraphTab'

import { i18nT } from '../i18n/t'
const TABS = [
  { key: 'logs', label: 'Logs', icon: <ScrollText size={16} />, description: 'Live log viewer with level filtering and search' },
  { key: 'system', label: 'System', icon: <Monitor size={16} />, description: 'CPU, memory, network, and process metrics' },
  { key: 'telemetry', label: 'Telemetry', icon: <Activity size={16} />, description: 'Session startup latency (p50/p90) and MCP/skill acceleration metrics' },
  { key: 'storage', label: 'Storage', icon: <Database size={16} />, description: 'localStorage usage, quotas, and garbage collection' },
  { key: 'mcp-pool', label: 'MCP Pool', icon: <Network size={16} />, description: 'Shared MCP gateway and poolable server configuration' },
  { key: 'memory', label: 'Memory', icon: <Brain size={16} />, description: 'Memory graph, embedding provider, and vector store internals' },
  { key: 'config', label: 'Config', icon: <FileCode2 size={16} />, description: 'KiroCrew and agent configuration viewers (read-only)' },
  { key: 'archive', label: 'Archive', icon: <Archive size={16} />, description: 'Rotated/compacted session history (7-day retention)' },
]

export default function DeveloperPage() {
  return (
    <SidePanelLayout title={i18nT('pages.developerPage.developer')} tabs={TABS}>
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
            {/* The memory GRAPH visualizer moved here from Settings > Overview
                (mission-control rewrite) — it is an internals view. The
                user-facing memory browser (settings, preferences, projects,
                history, lessons + vector store card) stays in Settings >
                Overview > Memory. */}
            <MemoryGraphTab />
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
