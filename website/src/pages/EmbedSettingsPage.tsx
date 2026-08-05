import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Palette, MessageSquare, Bell } from 'lucide-react'
import { useAppSelector } from '../store'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'

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

      {/* Tab bar */}
      <div className="flex gap-1 px-4 py-2 border-b border-border shrink-0 overflow-x-auto">
        {tabs.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
              tab === t.key
                ? 'bg-accent/10 text-accent'
                : 'text-muted hover:text-text hover:bg-bg-hover'
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {/* Panel content */}
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-4">
        {tab === 'display' && <DisplayPanel />}
        {tab === 'chat' && <ChatPanel />}
        {tab === 'notifications' && <NotificationsPanel />}
      </div>
    </div>
  )
}
