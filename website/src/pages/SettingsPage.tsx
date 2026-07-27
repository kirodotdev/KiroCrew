import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bell, Code, Globe, Import, Info, Keyboard, Link2, MessageSquare, Mic, Palette, PanelsTopLeft, Server, ShieldCheck } from 'lucide-react'
import { useAppSelector } from '../store'
import SidePanelLayout from '../components/SidePanelLayout'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { BrowserPanel } from './settings/BrowserPanel'
import { InstancesPanel } from './settings/InstancesPanel'
import { isEmbeddedPane } from '../lib/embedded'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { VoicePanel } from './settings/VoicePanel'
import { DeveloperPanel } from './settings/DeveloperPanel'
import { SecurityPanel } from './settings/SecurityPanel'
import { ChannelsPanel, CHANNEL_KEYS } from './settings/ChannelsPanel'
import { OverviewPanel } from './settings/OverviewPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'
import { ShortcutsPanel } from './settings/ShortcutsPanel'
import { AboutPanel } from './settings/AboutPanel'
import { ImportPanel } from './settings/ImportPanel'

const GROUP_PREFERENCES = 'Preferences'
const GROUP_SYSTEM = 'System'

const TABS = [
  { key: 'overview', label: 'Overview', icon: <PanelsTopLeft size={16} />, description: 'System health, activity, and usage & memory at a glance' },
  { key: 'imports', label: 'Import / Export', icon: <Import size={16} />, description: 'Bring data from another AI agent, and back up or restore KiroCrew configuration' },
  { key: 'chat', label: 'Chat', icon: <MessageSquare size={16} />, group: GROUP_PREFERENCES, description: 'Message behavior, history, timestamps, and context' },
  { key: 'display', label: 'Display', icon: <Palette size={16} />, group: GROUP_PREFERENCES, description: 'Zoom, font, and color theme preferences' },
  { key: 'voice', label: 'Voice', icon: <Mic size={16} />, group: GROUP_PREFERENCES, description: 'Text-to-speech and speech-to-text (dictation) settings' },
  { key: 'notifications', label: 'Notifications', icon: <Bell size={16} />, group: GROUP_PREFERENCES, description: 'Sound effects and per-source alert preferences' },
  { key: 'shortcuts', label: 'Shortcuts', icon: <Keyboard size={16} />, group: GROUP_PREFERENCES, description: 'Keyboard shortcuts reference and preferences' },
  { key: 'channels', label: 'Channels', icon: <Link2 size={16} />, description: 'Chat platforms the agent can send and receive on — Slack, Discord, Telegram, Webex, WeCom' },
  { key: 'browser', label: 'Browser', icon: <Globe size={16} />, group: GROUP_SYSTEM, description: 'Playwright browser mode, extension token, and auth configuration' },
  { key: 'instances', label: 'Instances', icon: <Server size={16} />, group: GROUP_SYSTEM, description: 'Manage remote KiroCrew instances over SSH tunnels; switch between them from the top header' },
  { key: 'security', label: 'Security', icon: <ShieldCheck size={16} />, group: GROUP_SYSTEM, description: 'Security posture, defense layers, certifications, and data classification' },
  { key: 'developer', label: 'Developer', icon: <Code size={16} />, group: GROUP_SYSTEM, description: 'Developer mode, logs, system metrics, and diagnostics' },
  { key: 'about', label: 'About', icon: <Info size={16} />, dividerBefore: true, description: 'Version, update channel, check for updates, and license' },
]

export default function SettingsPage() {
  const version = useAppSelector(s => s.dashboard.status?.version) || '—'
  useSettingHighlight()
  const [params, setParams] = useSearchParams()

  // Legacy deep-link remap: the five per-channel tabs collapsed into one
  // Channels tab. ?tab=slack (bookmarks, command palette history, docs)
  // becomes ?tab=channels&channel=slack. Plain useEffect on purpose:
  // react-router 7 drops navigations fired from useLayoutEffect during the
  // initial mount (its ready flag is set in a passive effect), so the remap
  // must run as a passive effect too. Until it fires, SidePanelLayout treats
  // the unknown tab as the default (Overview) for one frame.
  const rawTab = params.get('tab')
  useEffect(() => {
    if (rawTab && CHANNEL_KEYS.includes(rawTab)) {
      setParams(prev => {
        const next = new URLSearchParams(prev)
        next.set('tab', 'channels')
        next.set('channel', rawTab)
        return next
      }, { replace: true })
    }
  }, [rawTab, setParams])

  // An embedded instance pane can't manage remote instances (single-level by
  // design) — hide the Instances tab so a pane can't connect onward.
  const embedded = isEmbeddedPane()
  // Update nudge: dot on the About entry while a desktop update is available
  // (mirrored from Electron update-state by useUpdateSubscription).
  const updateAvailable = useAppSelector(s => s.dashboard.desktopUpdateAvailable)
  const baseTabs = embedded ? TABS.filter(t => t.key !== 'instances') : TABS
  const tabs = updateAvailable ? baseTabs.map(t => (t.key === 'about' ? { ...t, dot: true } : t)) : baseTabs

  return (
    <SidePanelLayout
      title="Settings"
      tabs={tabs}
      footer={<span className="text-[12px] text-muted">KiroCrew v{version}</span>}
    >
      {tab => <>
        {tab === 'overview' && <OverviewPanel />}
        {tab === 'imports' && <ImportPanel />}
        {tab === 'chat' && <ChatPanel />}
        {tab === 'display' && <DisplayPanel />}
        {tab === 'voice' && <VoicePanel />}
        {tab === 'notifications' && <NotificationsPanel />}
        {tab === 'shortcuts' && <ShortcutsPanel />}
        {tab === 'channels' && <ChannelsPanel />}
        {tab === 'browser' && <BrowserPanel />}
        {tab === 'instances' && !embedded && <InstancesPanel />}
        {tab === 'security' && <SecurityPanel />}
        {tab === 'developer' && <DeveloperPanel />}
        {tab === 'about' && <AboutPanel />}
      </>}
    </SidePanelLayout>
  )
}
