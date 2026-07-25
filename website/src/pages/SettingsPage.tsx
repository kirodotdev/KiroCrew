import { Bell, Code, Globe, Info, Keyboard, MessageSquare, Mic, Palette, PanelsTopLeft, Server, ShieldCheck } from 'lucide-react'
import { SlackIcon } from '../components/SlackIcon'
import { useAppSelector } from '../store'
import SidePanelLayout from '../components/SidePanelLayout'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { BrowserPanel } from './settings/BrowserPanel'
import { InstancesPanel } from './settings/InstancesPanel'
import { isEmbeddedPane } from '../lib/embedded'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { VoicePanel } from './settings/VoicePanel'
import { GeneralPanel } from './settings/GeneralPanel'
import { SecurityPanel } from './settings/SecurityPanel'
import { SlackPanel } from './settings/SlackPanel'
import { DiscordPanel } from './settings/DiscordPanel'
import { DiscordIcon } from '../components/DiscordIcon'
import { TelegramPanel } from './settings/TelegramPanel'
import { WebexPanel } from './settings/WebexPanel'
import { WebexIcon } from '../components/WebexIcon'
import { TelegramLogo } from '../components/TelegramLogo'
import { WeComPanel } from './settings/WeComPanel'
import { WeComLogo } from '../components/WeComLogo'
import { OverviewPanel } from './settings/OverviewPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'
import { ShortcutsPanel } from './settings/ShortcutsPanel'
import { AboutPanel } from './settings/AboutPanel'

const TABS = [
  { key: 'overview', label: 'Overview', icon: <PanelsTopLeft size={16} />, description: 'System status, memory, agent config, and usage metrics' },
  { key: 'chat', label: 'Chat', icon: <MessageSquare size={16} />, description: 'Message behavior, history, timestamps, and context' },
  { key: 'voice', label: 'Voice', icon: <Mic size={16} />, description: 'Text-to-speech and speech-to-text (dictation) settings' },
  { key: 'display', label: 'Display', icon: <Palette size={16} />, description: 'Zoom, font, and color theme preferences' },
  { key: 'browser', label: 'Browser', icon: <Globe size={16} />, description: 'Playwright browser mode, extension token, and auth configuration' },
  { key: 'instances', label: 'Instances', icon: <Server size={16} />, description: 'Manage remote KiroCrew instances over SSH tunnels; switch between them from the top header' },
  { key: 'security', label: 'Security', icon: <ShieldCheck size={16} />, description: 'Security posture, defense layers, certifications, and data classification' },
  { key: 'notifications', label: 'Notifications', icon: <Bell size={16} />, description: 'Sound effects and per-category alert preferences' },
  { key: 'slack', label: 'Slack', icon: <SlackIcon size={16} />, description: 'Slack channel integration settings' },
  { key: 'discord', label: 'Discord', icon: <DiscordIcon size={16} />, description: 'Discord bot channel integration settings' },
  { key: 'telegram', label: 'Telegram', icon: <TelegramLogo size={16} />, description: 'Telegram bot channel integration settings' },
  { key: 'webex', label: 'Webex', icon: <WebexIcon size={16} />, description: 'Webex channel integration settings' },
  { key: 'wecom', label: 'WeCom', icon: <WeComLogo size={16} />, description: 'WeCom (WeChat Work) channel integration settings' },
  { key: 'shortcuts', label: 'Shortcuts', icon: <Keyboard size={16} />, description: 'Keyboard shortcuts reference and preferences' },
  { key: 'developer', label: 'Developer', icon: <Code size={16} />, description: 'Developer mode, logs, system metrics, and diagnostics' },
  { key: 'about', label: 'About', icon: <Info size={16} />, description: 'Version, update channel, check for updates, and license' },
]

export default function SettingsPage() {
  const version = useAppSelector(s => s.dashboard.status?.version) || '—'
  useSettingHighlight()
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
        {tab === 'chat' && <ChatPanel />}
        {tab === 'voice' && <VoicePanel />}
        {tab === 'display' && <DisplayPanel />}
        {tab === 'browser' && <BrowserPanel />}
        {tab === 'instances' && !embedded && <InstancesPanel />}
        {tab === 'security' && <SecurityPanel />}
        {tab === 'notifications' && <NotificationsPanel />}
        {tab === 'slack' && <SlackPanel />}
        {tab === 'discord' && <DiscordPanel />}
        {tab === 'telegram' && <TelegramPanel />}
        {tab === 'webex' && <WebexPanel />}
        {tab === 'wecom' && <WeComPanel />}
        {tab === 'shortcuts' && <ShortcutsPanel />}
        {tab === 'developer' && <GeneralPanel />}
        {tab === 'about' && <AboutPanel />}
        {tab !== 'overview' && tab !== 'chat' && tab !== 'voice' && tab !== 'display' && tab !== 'browser' && tab !== 'instances' && tab !== 'security' && tab !== 'notifications' && tab !== 'slack' && tab !== 'discord' && tab !== 'telegram' && tab !== 'webex' && tab !== 'wecom' && tab !== 'shortcuts' && tab !== 'developer' && tab !== 'about' && (
          <div className="text-muted text-sm py-12 text-center">
            {TABS.find(t => t.key === tab)?.label} settings — coming soon
          </div>
        )}
      </>}
    </SidePanelLayout>
  )
}
