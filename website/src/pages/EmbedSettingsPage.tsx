import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Palette, MessageSquare, Bell } from 'lucide-react'
import { useAppSelector } from '../store'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'

import { i18nT } from '../i18n/t'

/**
 * A FUNCTION, not a module-level array: a module-level constant is evaluated
 * once at import, so an `i18nT()` call there would freeze the boot language and
 * leave the tab bar English after a language switch. Called once per render.
 *
 * The labels REUSE the settings-page tab keys rather than adding duplicates:
 * these three tabs render the very same DisplayPanel / ChatPanel /
 * NotificationsPanel that `settings.tabs.{display,chat,notifications}` name in
 * the full settings rail, so the two must not be able to drift apart.
 */
function buildTabs() {
  return [
    { key: 'display', label: i18nT('settings.tabs.display.label'), icon: <Palette size={14} /> },
    { key: 'chat', label: i18nT('settings.tabs.chat.label'), icon: <MessageSquare size={14} /> },
    { key: 'notifications', label: i18nT('settings.tabs.notifications.label'), icon: <Bell size={14} /> },
  ]
}

export default function EmbedSettingsPage() {
  const navigate = useNavigate()
  const [tab, setTab] = useState('display')
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const tabs = buildTabs()

  const goBack = () => {
    if (activeSlot) {
      navigate(`/embed/chat/${activeSlot}?sid=${activeSlot}`)
    } else {
      navigate('/embed/sessions')
    }
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Header with back button */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border shrink-0">
        <button
          onClick={goBack}
          className="p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
          aria-label={i18nT('pages.embedSettingsPage.back')}
        >
          <ArrowLeft size={16} />
        </button>
        <span className="text-sm font-medium">{i18nT('pages.embedSettingsPage.settings')}</span>
      </div>

      {/* Tab bar: the shared Radix rail, so a screen reader hears which tab
          is selected and the arrow keys move between tabs. Each panel is a
          TabsContent, which Radix unmounts while inactive. */}
      <Tabs value={tab} onValueChange={setTab} layoutId="embed-settings-tabs" className="flex-1 min-h-0 flex flex-col">
        <div className="px-4 py-2 border-b border-border shrink-0 overflow-x-auto">
          <TabsList aria-label={i18nT('pages.embedSettingsPage.settings')}>
            {tabs.map(t => (
              <TabsTrigger key={t.key} value={t.key}>
                {t.icon}
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        {/* Panel content */}
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-4">
          <TabsContent value="display"><DisplayPanel /></TabsContent>
          <TabsContent value="chat"><ChatPanel /></TabsContent>
          <TabsContent value="notifications"><NotificationsPanel /></TabsContent>
        </div>
      </Tabs>
    </div>
  )
}
