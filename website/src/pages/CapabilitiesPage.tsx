import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Link2, BookOpen, Users, MessageSquareText, Webhook, LayoutTemplate, Compass, Workflow, Library } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import ErrorBoundary from '../components/ErrorBoundary'
import RestartButton from '../components/RestartButton'
import { useProvider } from '../providers'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import AgentsPage from './AgentsPage'
import KiroCrewAgentsPage from './KiroCrewAgentsPage'
import HooksPage from './HooksPage'
import ConnectionsPage from './connections/ConnectionsPage'
import KnowledgePage from './KnowledgePage'
import { SkillsTab, PromptsTab, SteeringTab } from './overview'
import WorkflowLibraryTab from './overview/WorkflowLibraryTab'


/**
 * Opt-in flag for the Connections services gallery.
 *
 * The Connections work (provider registry, OAuth relay, card gallery) is merged
 * on main but held for a later release, so the gallery must not be reachable in
 * a shipped build. When the flag is absent — the default for every install —
 * this tab renders the pre-existing MCP Servers table, exactly as it did before
 * the gallery landed.
 *
 * A flag rather than a revert because the team asked to keep the code on main
 * and test from there: set `connections_ui: true` in the running instance's
 * `$KIROCREW_HOME/config.json` to exercise the gallery locally. Config is read
 * live, so no gateway restart is needed. The predicate lives in
 * hooks/useConnectionsUi so chat's banner gate reads the same answer.
 */

export default function CapabilitiesPage() {
  const provider = useProvider()
  const { t } = useTranslation()

  const connectionsUiEnabled = useConnectionsUiEnabled()

  const tabs = useMemo(() => {
    // Three-group rail. Groups are display labels (SidePanelLayout keys group
    // membership on string identity), so each is computed once per render and
    // shared across its tabs; the memo below re-runs on language change via `t`.
    const groupAgent = t('pages.capabilitiesPage.group_agent')
    const groupKnowledge = t('pages.capabilitiesPage.group_knowledge_instructions')
    const groupAutomation = t('pages.capabilitiesPage.group_automation')
    return [
      { key: 'crews', label: t('pages.capabilitiesPage.crews_label'), icon: <Users size={16} />, description: t('pages.capabilitiesPage.crews_description'), group: groupAgent },
      { key: 'templates', label: t('pages.capabilitiesPage.templates_label'), icon: <LayoutTemplate size={16} />, description: t('pages.capabilitiesPage.templates_description'), group: groupAgent },
      { key: 'skills', label: t('pages.capabilitiesPage.skills_label'), icon: <BookOpen size={16} />, description: t('pages.capabilitiesPage.skills_description'), group: groupAgent },
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
  }, [provider, t])

  return (
    <SidePanelLayout title={t('pages.capabilitiesPage.agent_capabilities')} tabs={tabs} rememberKey="capabilities" headerRight={<RestartButton />}>
      {tab => <>
        {tab === 'crews' && <KiroCrewAgentsPage embedded />}
        {tab === 'templates' && <AgentsPage embedded />}
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
