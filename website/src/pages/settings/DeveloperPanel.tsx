import { safeSetItem } from '../../utils/safeStorage'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ExternalLink } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'

import { i18nT } from '../../i18n/t'
const DEV_MODE_KEY = 'mc-dev-mode'
const DEV_MODE_EVENT = 'mc-dev-mode-changed'

/** Settings > Developer tab.
 *
 *  Deliberately minimal: the Developer Mode toggle is a consent gate, and the
 *  hardcore internals it unlocks (logs, system metrics, memory internals,
 *  MCP pool/gateway controls) live on the standalone Developer PAGE behind
 *  that gate — not in always-visible Settings. Early-access updates are handled
 *  by the stable | insider channel switcher in Settings > About, so this tab
 *  carries no beta-channel toggle. */
export function DeveloperPanel() {
  const navigate = useNavigate()
  const [devMode, setDevMode] = useState(() => localStorage.getItem(DEV_MODE_KEY) === '1')

  const toggleDevMode = (v: boolean) => {
    safeSetItem(DEV_MODE_KEY, v ? '1' : '0')
    setDevMode(v)
    window.dispatchEvent(new CustomEvent(DEV_MODE_EVENT, { detail: v }))
    // Notify Electron main process to show/hide DevTools menu item
    ;(window as Window & { electronAPI?: { setDevMode?: (v: boolean) => void } }).electronAPI?.setDevMode?.(v)
  }

  return (
    <SettingsSection title={i18nT('pages.settings.developerPanel.developer_tools')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.developerPanel.developer_mode')}
          description={i18nT('pages.settings.developerPanel.show_developer_page_in_sidebar_with_logs_system')}
          checked={devMode}
          onChange={toggleDevMode}
        />
        {devMode && (
          <div className="pt-1">
            <button
              type="button"
              onClick={() => navigate('/developer')}
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 hover:underline"
            >
              {i18nT('pages.settings.developerPanel.open_developer_page')}
              <ExternalLink size={13} className="lucide-inline" />
            </button>
          </div>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
