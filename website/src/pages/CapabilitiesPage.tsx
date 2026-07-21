import { useMemo } from 'react'
import { Plug, BookOpen, Users, MessageSquareText, Webhook, LayoutTemplate } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import RestartButton from '../components/RestartButton'
import { useProvider } from '../providers'
import AgentsPage from './AgentsPage'
import KiroCrewAgentsPage from './KiroCrewAgentsPage'
import HooksPage from './HooksPage'
import { McpTab, SkillsTab, PromptsTab } from './overview'

export default function CapabilitiesPage() {
  const provider = useProvider()

  const tabs = useMemo(() => {
    return [
      { key: 'agents', label: 'Agents', icon: <Users size={16} />, description: 'Manage agent → workspace → memory store bindings' },
      { key: 'templates', label: 'Agent Templates', icon: <LayoutTemplate size={16} />, description: `Installed agent configurations and packages` },
      { key: 'mcp', label: 'Integrations (MCP)', icon: <Plug size={16} />, description: 'Add tools that let your agent work with Slack, AWS, code repos, and other services' },
      { key: 'skills', label: 'Skills', icon: <BookOpen size={16} />, description: 'Specialized knowledge files your agent loads on demand for specific tasks' },
      { key: 'hooks', label: 'Hooks', icon: <Webhook size={16} />, description: 'Shell commands that run automatically on agent events like prompts, tool calls, and session start/stop' },
      { key: 'prompts', label: 'Prompts', icon: <MessageSquareText size={16} />, description: `Reusable prompt templates from ${provider.labels.pluginRegistryName || 'packages'}` },
    ]
  }, [provider])

  return (
    <SidePanelLayout title="Agent Capabilities" tabs={tabs} headerRight={<RestartButton />}>
      {tab => <>
        {tab === 'agents' && <KiroCrewAgentsPage embedded />}
        {tab === 'templates' && <AgentsPage embedded />}
        {tab === 'mcp' && <McpTab />}
        {tab === 'skills' && <SkillsTab />}
        {tab === 'hooks' && <HooksPage embedded />}
        {tab === 'prompts' && <PromptsTab />}
      </>}
    </SidePanelLayout>
  )
}
