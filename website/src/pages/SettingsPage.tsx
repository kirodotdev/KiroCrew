import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bell, Code, Fingerprint, Globe, Import, Info, Keyboard, Link2, MessageSquare, Mic, Palette, PanelsTopLeft, Server, ShieldCheck, Sparkles, SquareMousePointer } from 'lucide-react'
import { useAppSelector } from '../store'
import SidePanelLayout from '../components/SidePanelLayout'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { BrowserPanel } from './settings/BrowserPanel'
import { RemoteCrewPanel } from './settings/RemoteCrewPanel'
import { isEmbeddedPane } from '../lib/embedded'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { SkillsPanel } from './settings/SkillsPanel'
import { VoicePanel } from './settings/VoicePanel'
import { DeveloperPanel } from './settings/DeveloperPanel'
import { SecurityPanel } from './settings/SecurityPanel'
import { ChannelsPanel, CHANNEL_KEYS } from './settings/ChannelsPanel'
import { OverviewPanel } from './settings/OverviewPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'
import { ShortcutsPanel } from './settings/ShortcutsPanel'
import { AboutPanel } from './settings/AboutPanel'
import { ImportPanel } from './settings/ImportPanel'
import { ComputerUsePanel } from './settings/ComputerUsePanel'
import { PrivacyPanel } from './settings/PrivacyPanel'

import { i18nT } from '../i18n/t'
// Group headers double as the grouping KEY (SidePanelLayout starts a new header
// whenever this string changes), so they are resolved inside `buildTabs()` —
// which runs per render — rather than at module load. Translating them at module
// scope would freeze the header in the boot language while the tabs switched.

/**
 * Settings tab descriptors.
 *
 * A FUNCTION, not a module-level array: the labels/descriptions are translated,
 * and a module-level constant would freeze them in whichever language was active
 * at import time — leaving the tab rail English after a language switch. Called
 * once per render inside the component instead.
 *
 * The group headers double as the grouping KEY (SidePanelLayout starts a new
 * header whenever this string changes), so they are resolved here too.
 */
function buildTabs() {
  const GROUP_PREFERENCES = i18nT('settings.groups.preferences')
  const GROUP_SYSTEM = i18nT('settings.groups.system')
  return [
    { key: 'overview', label: i18nT('settings.tabs.overview.label'), icon: <PanelsTopLeft size={16} />, description: i18nT('settings.tabs.overview.description') },
    { key: 'imports', label: i18nT('settings.tabs.imports.label'), icon: <Import size={16} />, description: i18nT('settings.tabs.imports.description') },
    { key: 'chat', label: i18nT('settings.tabs.chat.label'), icon: <MessageSquare size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.chat.description') },
    { key: 'display', label: i18nT('settings.tabs.display.label'), icon: <Palette size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.display.description') },
    { key: 'voice', label: i18nT('settings.tabs.voice.label'), icon: <Mic size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.voice.description') },
    { key: 'notifications', label: i18nT('settings.tabs.notifications.label'), icon: <Bell size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.notifications.description') },
    { key: 'shortcuts', label: i18nT('settings.tabs.shortcuts.label'), icon: <Keyboard size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.shortcuts.description') },
    { key: 'skills', label: i18nT('settings.tabs.skills.label'), icon: <Sparkles size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.skills.description') },
    { key: 'channels', label: i18nT('settings.tabs.channels.label'), icon: <Link2 size={16} />, description: i18nT('settings.tabs.channels.description') },
    { key: 'browser', label: i18nT('settings.tabs.browser.label'), icon: <Globe size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.browser.description') },
    { key: 'computer-use', label: i18nT('settings.tabs.computerUse.label'), icon: <SquareMousePointer className="lucide-inline" />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.computerUse.description') },
    { key: 'instances', label: i18nT('settings.tabs.instances.label'), icon: <Server size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.instances.description') },
    { key: 'privacy', label: i18nT('privacyDisclosure.settingsLabel'), icon: <Fingerprint className="lucide-inline" />, group: GROUP_SYSTEM, description: i18nT('privacyDisclosure.settingsDescription') },
    { key: 'security', label: i18nT('settings.tabs.security.label'), icon: <ShieldCheck size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.security.description') },
    { key: 'developer', label: i18nT('settings.tabs.developer.label'), icon: <Code size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.developer.description') },
    { key: 'about', label: i18nT('settings.tabs.about.label'), icon: <Info size={16} />, dividerBefore: true, description: i18nT('settings.tabs.about.description') },
  ]
}

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
  const allTabs = buildTabs()
  const baseTabs = embedded ? allTabs.filter(t => t.key !== 'instances') : allTabs
  const tabs = updateAvailable ? baseTabs.map(t => (t.key === 'about' ? { ...t, dot: true } : t)) : baseTabs

  return (
    <SidePanelLayout
      title={i18nT('pages.settingsPage.settings')}
      tabs={tabs}
      // Keyed apart from the main window: an embedded pane has a different tab
      // roster (no Instances), so the two must not restore each other's tab.
      rememberKey={embedded ? 'settings-embedded' : 'settings'}
      footer={<span className="text-[12px] text-muted">{i18nT('pages.settingsPage.kirocrew_v')}{version}</span>}
    >
      {tab => <>
        {tab === 'overview' && <OverviewPanel />}
        {tab === 'imports' && <ImportPanel />}
        {tab === 'chat' && <ChatPanel />}
        {tab === 'display' && <DisplayPanel />}
        {tab === 'voice' && <VoicePanel />}
        {tab === 'notifications' && <NotificationsPanel />}
        {tab === 'shortcuts' && <ShortcutsPanel />}
        {tab === 'skills' && <SkillsPanel />}
        {tab === 'channels' && <ChannelsPanel />}
        {tab === 'browser' && <BrowserPanel />}
        {tab === 'computer-use' && <ComputerUsePanel />}
        {tab === 'instances' && !embedded && <RemoteCrewPanel />}
        {tab === 'privacy' && <PrivacyPanel />}
        {tab === 'security' && <SecurityPanel />}
        {tab === 'developer' && <DeveloperPanel />}
        {tab === 'about' && <AboutPanel />}
      </>}
    </SidePanelLayout>
  )
}
