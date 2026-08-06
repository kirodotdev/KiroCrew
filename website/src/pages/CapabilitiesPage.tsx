import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link2, BookOpen, Users, MessageSquareText, Webhook, LayoutTemplate, Compass } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import RestartButton from '../components/RestartButton'
import { useProvider } from '../providers'
import { api } from '../api/client'
import AgentsPage from './AgentsPage'
import KiroCrewAgentsPage from './KiroCrewAgentsPage'
import HooksPage from './HooksPage'
import ConnectionsPage from './connections/ConnectionsPage'
import { SkillsTab, PromptsTab, SteeringTab } from './overview'

import { i18nT } from '../i18n/t'

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
 * live, so no gateway restart is needed.
 */
const CONNECTIONS_UI_FLAG = 'connections_ui'

export default function CapabilitiesPage() {
  const provider = useProvider()

  // Absent config, a failed fetch, and a non-boolean value all resolve to
  // false: the gallery is reachable only on an explicit opt-in.
  const { data: kirocrewConfig } = useQuery({
    queryKey: ['kirocrew-config', CONNECTIONS_UI_FLAG],
    queryFn: () => api.kirocrewConfig(),
  })
  const connectionsUiEnabled =
    (kirocrewConfig as Record<string, unknown> | undefined)?.[CONNECTIONS_UI_FLAG] === true

  const tabs = useMemo(() => {
    return [
      { key: 'crews', label: i18nT('pages.capabilitiesPage.crews_label'), icon: <Users size={16} />, description: i18nT('pages.capabilitiesPage.crews_description') },
      { key: 'templates', label: i18nT('pages.capabilitiesPage.templates_label'), icon: <LayoutTemplate size={16} />, description: i18nT('pages.capabilitiesPage.templates_description') },
      // The label and description are deliberately unchanged. Substituting the
      // pre-gallery "MCP Servers" strings was tried and reverted: those keys were
      // renamed when the gallery landed and no catalog still resolves them, so
      // the render-time i18n gate correctly caught a raw key leaking into the
      // tab label. Wording is a follow-up; what matters for the release is that
      // the gallery itself is unreachable.
      { key: 'mcp', label: i18nT('pages.capabilitiesPage.connections_label'), icon: <Link2 size={16} />, description: i18nT('pages.capabilitiesPage.connections_description') },
      { key: 'skills', label: i18nT('pages.capabilitiesPage.skills_label'), icon: <BookOpen size={16} />, description: i18nT('pages.capabilitiesPage.skills_description') },
      { key: 'steering', label: i18nT('pages.capabilitiesPage.steering_label'), icon: <Compass size={16} />, description: i18nT('pages.capabilitiesPage.steering_description') },
      { key: 'hooks', label: i18nT('pages.capabilitiesPage.hooks_label'), icon: <Webhook size={16} />, description: i18nT('pages.capabilitiesPage.hooks_description') },
      { key: 'prompts', label: i18nT('pages.capabilitiesPage.prompts_label'), icon: <MessageSquareText size={16} />, description: i18nT('pages.capabilitiesPage.prompts_description', { registry: provider.labels.pluginRegistryName || 'packages' }) },
    ]
  }, [provider])

  return (
    <SidePanelLayout title={i18nT('pages.capabilitiesPage.agent_capabilities')} tabs={tabs} rememberKey="capabilities" headerRight={<RestartButton />}>
      {tab => <>
        {tab === 'crews' && <KiroCrewAgentsPage embedded />}
        {tab === 'templates' && <AgentsPage embedded />}
        {tab === 'mcp' && <ConnectionsPage servicesEnabled={connectionsUiEnabled} />}
        {tab === 'skills' && <SkillsTab />}
        {tab === 'steering' && <SteeringTab />}
        {tab === 'hooks' && <HooksPage embedded />}
        {tab === 'prompts' && <PromptsTab />}
      </>}
    </SidePanelLayout>
  )
}
