import { safeSetItem } from '../../utils/safeStorage'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ExternalLink } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'

const DEV_MODE_KEY = 'mc-dev-mode'
const DEV_MODE_EVENT = 'mc-dev-mode-changed'

/** Settings > Developer tab.
 *
 *  Deliberately minimal: the Developer Mode toggle is a consent gate, and the
 *  hardcore internals it unlocks (logs, system metrics, memory internals,
 *  MCP pool/gateway controls) live on the standalone Developer PAGE behind
 *  that gate — not in always-visible Settings. The tab's former "Beta
 *  Channel (Braveheart)" toggle was deleted outright: braveheart was the
 *  pre-GitHub integration branch, and the shipped channel model (stable |
 *  insider switcher in Settings > About) already covers early-access
 *  updates. */
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
    <SettingsSection title="Developer Tools">
      <SettingsCard>
        <SettingsToggle
          label="Developer Mode"
          description="Show Developer page in sidebar with Logs, System metrics, and Memory internals"
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
              Open Developer page
              <ExternalLink size={13} className="lucide-inline" />
            </button>
          </div>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
