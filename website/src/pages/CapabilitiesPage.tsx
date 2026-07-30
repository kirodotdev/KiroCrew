import { useMemo } from 'react'
import { Plug, BookOpen, Users, MessageSquareText, Webhook, LayoutTemplate, Compass } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import RestartButton from '../components/RestartButton'
import { useProvider } from '../providers'
import AgentsPage from './AgentsPage'
import KiroCrewAgentsPage from './KiroCrewAgentsPage'
import HooksPage from './HooksPage'
import { McpTab, SkillsTab, PromptsTab, SteeringTab } from './overview'

import { i18nT } from '../i18n/t'
export default function CapabilitiesPage() {
  const provider = useProvider()

  const tabs = useMemo(() => {
    return [
      { key: 'crews', label: 'Crews', icon: <Users size={16} />, description: 'Crews you chat with, each with its own workspace and memory' },
      { key: 'templates', label: 'Agent Templates', icon: <LayoutTemplate size={16} />, description: `Installed agent configurations and packages` },
      { key: 'mcp', label: 'Integrations(MCP)', icon: <Plug size={16} />, description: 'Add tools that let your agent work with Slack, AWS, code repos, and other services' },
      { key: 'skills', label: 'Skills', icon: <BookOpen size={16} />, description: 'Specialized knowledge files your agent loads on demand for specific tasks' },
      { key: 'steering', label: 'Steering', icon: <Compass size={16} />, description: 'Always-on markdown conventions from ~/.kiro/steering and your project\u2019s .kiro/steering' },
      { key: 'hooks', label: 'Hooks', icon: <Webhook size={16} />, description: 'Shell commands that run automatically on agent events like prompts, tool calls, and session start/stop' },
      { key: 'prompts', label: 'Prompts', icon: <MessageSquareText size={16} />, description: `Reusable prompt templates from ${provider.labels.pluginRegistryName || 'packages'}` },
    ]
  }, [provider])

  return (
    <SidePanelLayout title={i18nT('pages.capabilitiesPage.agent_capabilities')} tabs={tabs} headerRight={<RestartButton />}>
      {tab => <>
        {tab === 'crews' && <KiroCrewAgentsPage embedded />}
        {tab === 'templates' && <AgentsPage embedded />}
        {tab === 'mcp' && <McpTab />}
        {tab === 'skills' && <SkillsTab />}
        {tab === 'steering' && <SteeringTab />}
        {tab === 'hooks' && <HooksPage embedded />}
        {tab === 'prompts' && <PromptsTab />}
      </>}
    </SidePanelLayout>
  )
}
