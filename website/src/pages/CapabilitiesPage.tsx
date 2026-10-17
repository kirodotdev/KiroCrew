import { lazy, Suspense, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { Link2, BookOpen, Users, MessageSquareText, Webhook, Compass, Workflow, Library, FileCode2 } from 'lucide-react'
import { api } from '../api/client'
import SidePanelLayout from '../components/SidePanelLayout'
import ErrorBoundary from '../components/ErrorBoundary'
import ErrorNotice from '../components/ErrorNotice'
import { PinSurfaceButton } from '../components/PinSurfaceButton'
import { useProvider } from '../providers'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import KiroCrewAgentsPage from './KiroCrewAgentsPage'
import HooksPage from './HooksPage'
import ConnectionsPage from './connections/ConnectionsPage'
import KnowledgePage from './KnowledgePage'
import { SkillsTab, PromptsTab, SteeringTab } from './overview'
import WorkflowLibraryTab from './overview/WorkflowLibraryTab'
import { ContentSkeleton } from '../components/ui'

// The custom-agent editor is a drill-in most sessions never open; its chunk is
// fetched on the first visit rather than riding in the dashboard shell.
const AgentTemplatesTab = lazy(() => import('./overview/AgentTemplatesTab'))


/**
 * Escape hatch for the Connections services gallery.
 *
 * The Connections work (provider registry, OAuth relay, card gallery) ships ON:
 * with no `connections_ui` key — the state of every install that never touched
 * it — this tab's Services panel offers the launched provider cards. Setting
 * `connections_ui: false` in the running instance's `$KIROCREW_HOME/config.json`
 * empties that panel again, leaving the MCP Servers sub-tab as the only way to
 * reach a server. Config is read live, so no gateway restart is needed either
 * way.
 *
 * The predicate lives in hooks/useConnectionsUi so chat's banner gate reads the
 * same answer. WHICH providers a launched gallery offers is decided per provider
 * in pages/connections/registry.ts, not here.
 */

export default function CapabilitiesPage() {
  const provider = useProvider()
  const { t } = useTranslation()

  const connectionsUiEnabled = useConnectionsUiEnabled()

  // Generated skill candidates wait for approval in the Skills tab's review
  // queue, which only renders while that tab is open. The page remembers the
  // last tab and opens on Crewmates otherwise, so without a count on the
  // Skills row a waiting candidate is invisible from every other tab. Same key
  // and fetcher as the queue itself, so the two read one cache entry and the
  // `skills.pending_changed` frame refreshes both.
  const { data: pendingSkills, error: pendingSkillsError } = useQuery<{ pending?: unknown[] }>({
    queryKey: ['skills-pending'],
    queryFn: () => api.skillsPending(),
  })
  const pendingList = pendingSkills?.pending
  const pendingSkillCount = Array.isArray(pendingList) ? pendingList.length : 0
  // A failed read leaves the count at 0, which would read as "nothing is
  // waiting". The rail footer says the check failed instead, from every tab.
  const pendingSkillsNotice = pendingSkillsError ? (
    <>
      {/* No hand-off: the footer stays beside whichever tab is open, and the
          hand-off navigates to chat, unmounting that tab. A skill edit in
          progress lives only in the Skills tab's local state, with no leave
          guard to ask first, so the click would discard the draft. */}
      <ErrorNotice
        title={t('pages.capabilitiesPage.skills_awaiting_review_failed')}
        message={pendingSkillsError.message}
        messagePlacement="below"
        testId="capabilities-pending-skills-failure"
      />
    </>
  ) : undefined

  const tabs = useMemo(() => {
    // Three-group rail. Groups are display labels (SidePanelLayout keys group
    // membership on string identity), so each is computed once per render and
    // shared across its tabs; the memo below re-runs on language change via `t`.
    const groupAgent = t('pages.capabilitiesPage.group_agent')
    const groupKnowledge = t('pages.capabilitiesPage.group_knowledge_instructions')
    const groupAutomation = t('pages.capabilitiesPage.group_automation')
    return [
      { key: 'crews', label: t('pages.capabilitiesPage.crews_label'), icon: <Users size={16} />, description: t('pages.capabilitiesPage.crews_description'), group: groupAgent },
      // The custom agents crewmates and chats run. Beside Crewmates because a
      // crewmate is built from a custom agent; the tab manages the shared files
      // themselves.
      { key: 'templates', label: t('pages.capabilitiesPage.templates_label'), icon: <FileCode2 size={16} />, description: t('pages.capabilitiesPage.templates_description'), group: groupAgent },
      { key: 'skills', label: t('pages.capabilitiesPage.skills_label'), icon: <BookOpen size={16} />, description: t('pages.capabilitiesPage.skills_description'), group: groupAgent, badge: { count: pendingSkillCount, label: t('pages.capabilitiesPage.skills_awaiting_review', { count: pendingSkillCount }) } },
      // The label and description are deliberately unchanged. Substituting the
      // pre-gallery "MCP Servers" strings was tried and reverted: those keys were
      // renamed when the gallery landed and no catalog still resolves them, so
      // the render-time i18n gate correctly caught a raw key leaking into the
      // tab label. Wording is a follow-up; what matters for the release is that
      // the gallery itself is unreachable.
      { key: 'mcp', label: t('pages.capabilitiesPage.connections_label'), icon: <Link2 size={16} />, description: t('pages.capabilitiesPage.connections_description'), group: groupAgent },
      // Knowledge lives here rather than on the main rail: its consumer is the
      // agent (retrieval), and the human's intent on this surface is "manage
      // what the agent knows" — the same asset-management intent as Prompts and
      // Steering. The old /knowledge route redirects (App.tsx). `Library`, not
      // `BookOpen`: Skills already carries BookOpen in this same rail.
      // `fixedContent`: the page is a full-height flex shell (graph view,
      // Virtuoso-style internal scrolling), so its pane must contain it.
      { key: 'knowledge', label: t('pages.capabilitiesPage.knowledge_label'), icon: <Library size={16} />, description: t('pages.capabilitiesPage.knowledge_description'), group: groupKnowledge, fixedContent: true },
      { key: 'prompts', label: t('pages.capabilitiesPage.prompts_label'), icon: <MessageSquareText size={16} />, description: t('pages.capabilitiesPage.prompts_description', { registry: provider.labels.pluginRegistryName || 'packages' }), group: groupKnowledge },
      { key: 'steering', label: t('pages.capabilitiesPage.steering_label'), icon: <Compass size={16} />, description: t('pages.capabilitiesPage.steering_description'), group: groupKnowledge },
      { key: 'hooks', label: t('pages.capabilitiesPage.hooks_label'), icon: <Webhook size={16} />, description: t('pages.capabilitiesPage.hooks_description'), group: groupAutomation },
      { key: 'workflows', label: t('pages.capabilitiesPage.workflows_label'), icon: <Workflow className="lucide-inline" />, description: t('pages.capabilitiesPage.workflows_description'), group: groupAutomation },
    ]
    // `t` is a real dependency, not decoration: it subscribes to the language, so
    // a memo keyed only on `provider` would keep whichever language's labels it
    // first computed and the rail would stay in the old language after a switch.
  }, [provider, t, pendingSkillCount])

  return (
    <SidePanelLayout title={t('pages.capabilitiesPage.agent_capabilities')} tabs={tabs} rememberKey="capabilities" footer={pendingSkillsNotice} headerRight={<PinSurfaceButton defaultTab={tabs[0]?.key} />}>
      {tab => <>
        {tab === 'crews' && <KiroCrewAgentsPage embedded />}
        {/* ErrorBoundary around the lazy chunk: a stale chunk request after a
            deploy rejects the import, and without a local boundary that rejection
            would replace the whole dashboard with the app-shell error screen. */}
        {tab === 'templates' && <ErrorBoundary><Suspense fallback={<ContentSkeleton rows={6} />}><AgentTemplatesTab /></Suspense></ErrorBoundary>}
        {tab === 'mcp' && <ConnectionsPage servicesEnabled={connectionsUiEnabled} />}
        {tab === 'skills' && <SkillsTab />}
        {/* ErrorBoundary preserves the crash isolation the /knowledge route
            used to provide: the page lazy-loads the Graph chunk, and a stale
            chunk after a deploy would otherwise reject through to the root
            boundary and take the whole dashboard down with it. */}
        {tab === 'knowledge' && <ErrorBoundary><KnowledgePage embedded /></ErrorBoundary>}
        {tab === 'steering' && <SteeringTab />}
        {tab === 'hooks' && <HooksPage embedded />}
        {tab === 'prompts' && <PromptsTab />}
        {tab === 'workflows' && <WorkflowLibraryTab />}
      </>}
    </SidePanelLayout>
  )
}
