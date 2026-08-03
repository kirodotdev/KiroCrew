import { useMemo } from 'react'
import { Link2, BookOpen, Users, MessageSquareText, Webhook, LayoutTemplate, Compass } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import RestartButton from '../components/RestartButton'
import { useProvider } from '../providers'
import AgentsPage from './AgentsPage'
import KiroCrewAgentsPage from './KiroCrewAgentsPage'
import HooksPage from './HooksPage'
import ConnectionsPage from './connections/ConnectionsPage'
import { SkillsTab, PromptsTab, SteeringTab } from './overview'

import { i18nT } from '../i18n/t'
export default function CapabilitiesPage() {
  const provider = useProvider()

  const tabs = useMemo(() => {
    return [
      { key: 'crews', label: i18nT('pages.capabilitiesPage.crews_label'), icon: <Users size={16} />, description: i18nT('pages.capabilitiesPage.crews_description') },
      { key: 'templates', label: i18nT('pages.capabilitiesPage.templates_label'), icon: <LayoutTemplate size={16} />, description: i18nT('pages.capabilitiesPage.templates_description') },
      { key: 'mcp', label: i18nT('pages.capabilitiesPage.connections_label'), icon: <Link2 size={16} />, description: i18nT('pages.capabilitiesPage.connections_description') },
      { key: 'skills', label: i18nT('pages.capabilitiesPage.skills_label'), icon: <BookOpen size={16} />, description: i18nT('pages.capabilitiesPage.skills_description') },
      { key: 'steering', label: i18nT('pages.capabilitiesPage.steering_label'), icon: <Compass size={16} />, description: i18nT('pages.capabilitiesPage.steering_description') },
      { key: 'hooks', label: i18nT('pages.capabilitiesPage.hooks_label'), icon: <Webhook size={16} />, description: i18nT('pages.capabilitiesPage.hooks_description') },
      { key: 'prompts', label: i18nT('pages.capabilitiesPage.prompts_label'), icon: <MessageSquareText size={16} />, description: i18nT('pages.capabilitiesPage.prompts_description', { registry: provider.labels.pluginRegistryName || 'packages' }) },
    ]
  }, [provider])

  return (
    <SidePanelLayout title={i18nT('pages.capabilitiesPage.agent_capabilities')} tabs={tabs} headerRight={<RestartButton />}>
      {tab => <>
        {tab === 'crews' && <KiroCrewAgentsPage embedded />}
        {tab === 'templates' && <AgentsPage embedded />}
        {tab === 'mcp' && <ConnectionsPage />}
        {tab === 'skills' && <SkillsTab />}
        {tab === 'steering' && <SteeringTab />}
        {tab === 'hooks' && <HooksPage embedded />}
        {tab === 'prompts' && <PromptsTab />}
      </>}
    </SidePanelLayout>
  )
}
