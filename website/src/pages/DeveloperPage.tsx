import { lazy, Suspense, useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { ScrollText, Monitor, Brain, Archive, Database, Network, Activity, FileCode2, Bug, ArrowRight } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import { ContentSkeleton } from '../components/ui'
import { settingsPath } from '../components/settingsPath'
import type { SettingsTarget } from '../components/settingsPath'
import { SettingsLink } from '../components/SettingsLink'
import { FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR } from './settings/FeaturePreviewsSection'
import { AGENT_HARNESS_SETTINGS_TAB } from './developer/kiroSignInLink'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { LogViewer } from './LogsPage'
import SystemPage from './SystemPage'
import TelemetryPanel from './TelemetryPanel'
import SessionArchive from './SessionArchive'
import LocalStorageDebug from './LocalStorageDebug'
import { McpManagement } from './settings/McpManagement'
import { KiroCrewCfgTab, AgentCfgTab } from './overview'
import { DebugToolsTab } from './developer/DebugToolsTab'

/**
 * Lazy: MemoryGraphTab is the only eager owner of the sigma/graphology stack
 * (vendor-graph, ~180 KB gzip), which a static import keeps in the entry
 * modulepreload set for every page load even though this tab is one of nine on
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
    { key: 'debug-tools', label: i18nT('pages.developerPage.tabs.debugTools.label'), icon: <Bug size={16} />, description: i18nT('pages.developerPage.tabs.debugTools.description') },
    { key: 'archive', label: i18nT('pages.developerPage.tabs.archive.label'), icon: <Archive size={16} />, description: i18nT('pages.developerPage.tabs.archive.description') },
  ]
}

/**
 * The tab key Feature Previews had while it lived on this page — the one
 * string a bookmarked `/developer?tab=feature-previews` link still carries.
 */
const LEGACY_FEATURE_PREVIEWS_TAB = 'feature-previews'

/**
 * Where the cards live now: Settings > Developer, with the whole Feature
 * Previews section ringed so the reader sees exactly where the tab went rather
 * than landing on a pane and hunting. `key:` is useSettingHighlight's direct
 * `data-setting-key` lookup; the anchor is carried by the section's wrapper.
 * ONE target for both doors — the legacy redirect below and the rail footer's
 * signpost — so they cannot drift apart.
 */
const FEATURE_PREVIEWS_TARGET: SettingsTarget = {
  tab: 'developer',
  highlight: `key:${FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR}`,
}

/**
 * The tab key the coding-agent switch had while it lived on this page. It moved
 * to Settings > Agent Harness once first-run setup began offering agents other
 * than Kiro CLI: the sidebar shows this page only in Developer Mode, so a user
 * who set up with Claude Code had no visible way to switch agents or reach the
 * Kiro sign-in card under the switch. The old URL survives in bookmarks and in
 * already-open tabs that predate KIRO_SIGN_IN_PATH's move.
 */
const LEGACY_AGENT_BACKEND_TAB = 'agent-backend'

export default function DeveloperPage() {
  const tabs = buildTabs()
  const navigate = useNavigate()
  const { pathname, search } = useLocation()

  // Serves the default-crewmate deep link: the Crewmates roster's `default`
  // badge (`ChangeDefaultLink` in KiroCrewAgentsPage.tsx) points at
  // `DEFAULT_CREWMATE_PATH`, `/developer?tab=config&highlight=key:default-crewmate`,
  // and this hook rings the Default crewmate row on the Config tab.
  //
  // Gated on the route AND the tab: the legacy 'agent-backend' tab forwards its
  // `?highlight=` on to Settings > Agent Harness through the redirect below, so
  // this hook must leave that value alone for SettingsPage to consume rather
  // than finding no anchor here and stripping it first.
  useSettingHighlight(
    pathname === '/developer'
      && new URLSearchParams(search).get('tab') !== LEGACY_AGENT_BACKEND_TAB,
  )

  // Feature Previews moved to Settings > Developer. The old tab's URL survives
  // in bookmarks, docs and command-palette history, so a link that still names
  // it is translated with a REPLACE navigation (back must not land on the
  // pre-translation URL). Without this, SidePanelLayout would silently fall
  // back to the remembered or first tab, which reads as "the toggles are gone".
  // Passive useEffect on purpose, matching SettingsPage's legacy translation:
  // react-router 7 drops navigations fired from useLayoutEffect on first mount.
  //
  // The coding-agent switch moved the same way, to Settings > Agent Harness.
  // Its old links can carry `?highlight=key:kiro-sign-in` (the chat's "Sign in
  // to Kiro" row built exactly that URL), so the highlight rides along and
  // SettingsPage rings the sign-in card instead of this page consuming it.
  useEffect(() => {
    const qs = new URLSearchParams(search)
    const tab = qs.get('tab')
    if (tab === LEGACY_FEATURE_PREVIEWS_TAB) {
      navigate(settingsPath(FEATURE_PREVIEWS_TARGET), { replace: true })
    } else if (tab === LEGACY_AGENT_BACKEND_TAB) {
      const highlight = qs.get('highlight') ?? undefined
      navigate(settingsPath({ tab: AGENT_HARNESS_SETTINGS_TAB, highlight }), { replace: true })
    }
  }, [search, navigate])

  // The redirect only catches a URL that still names the old tab. A user who
  // navigates by memory — rail > Developer, then looks for the tab — finds it
  // gone with nothing pointing onward, and "Developer" now names two places
  // (this page and the Settings tab). The rail footer is the one spot every
  // tab of this page shares, so the signpost lives there and lands on the
  // same ringed section the redirect does.
  //
  // TIME-BOXED, not permanent: the need decays as users relearn, while the
  // redirect above covers bookmarks, docs and palette history indefinitely.
  // Retire the footer (and its catalog key) once two releases have shipped
  // with the move — tracked in kirodotdev/KiroCrew#9017 with the deletion
  // checklist and a due date.
  //
  // The rail is narrow, so the label wraps: plain inline text (not a flex row)
  // keeps the arrow glued to the last word instead of floating to the far edge.
  const footer = (
    <SettingsLink
      {...FEATURE_PREVIEWS_TARGET}
      className="text-[12px] leading-snug text-accent hover:underline"
    >
      {i18nT('pages.developerPage.feature_previews_moved')}
      {' '}
      <ArrowRight size={12} className="lucide-inline" />
    </SettingsLink>
  )

  return (
    <SidePanelLayout title={i18nT('pages.developerPage.developer')} tabs={tabs} rememberKey="developer" footer={footer}>
      {tab => <>
        {tab === 'logs' && <div className="h-[calc(100vh-160px)] min-h-[300px] flex flex-col overflow-hidden"><LogViewer compact /></div>}
        {tab === 'system' && <SystemPage embedded />}
        {tab === 'telemetry' && <TelemetryPanel />}
        {tab === 'storage' && <LocalStorageDebug />}
        {tab === 'mcp-pool' && <McpManagement />}
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
        {tab === 'debug-tools' && <DebugToolsTab />}
        {tab === 'archive' && <SessionArchive />}
      </>}
    </SidePanelLayout>
  )
}
