import { safeSetItem } from '../../utils/safeStorage'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ExternalLink } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { useLocalGateway } from '../../hooks/useLocalGateway'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'
import { CrewmatesSection } from './CrewmatesSection'
import { DefaultMcpGrantsSection } from './DefaultMcpGrantsSection'

import { i18nT } from '../../i18n/t'
const DEV_MODE_KEY = 'mc-dev-mode'
const DEV_MODE_EVENT = 'mc-dev-mode-changed'
/** The remote guide's SSH-tunnel section: it works on every desktop OS and
 *  covers both a hand-run forward and the app-held one (#3810). */
const LOCAL_GATEWAY_TUNNEL_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md#ssh-tunnel-laptop'

/** Settings > Developer tab.
 *
 *  Deliberately minimal: the Developer Mode toggle is a consent gate, and the
 *  hardcore internals it unlocks (logs, system metrics, memory internals,
 *  MCP pool/gateway controls) live on the standalone Developer PAGE behind
 *  that gate — not in always-visible Settings. Early-access updates are handled
 *  by the stable | insider channel switcher in Settings > About, so this tab
 *  carries no beta-channel toggle.
 *
 *  Feature Previews is the one thing that DID move here from that page: like
 *  Developer Mode it is a per-device opt-in switch, not an internals view, so
 *  it belongs beside the other consent gate rather than behind it — a reader
 *  should not have to unlock the Developer page to find out how to turn an
 *  unfinished feature on. Its cards live in `FeaturePreviewsSection.tsx`.
 *
 *  The Gateway section is desktop-app-only and appears only when the Electron
 *  bridge is present: a browser tab has no local gateway to start or stop. It
 *  sits here because it is an advanced switch with no other home yet, not
 *  because running remotely is a developer activity. */
export function DeveloperPanel() {
  const navigate = useNavigate()
  const [devMode, setDevMode] = useState(() => localStorage.getItem(DEV_MODE_KEY) === '1')
  const { localGatewayEnabled, localGatewaySupported, setLocalGatewayEnabled } = useLocalGateway()

  const toggleDevMode = (v: boolean) => {
    safeSetItem(DEV_MODE_KEY, v ? '1' : '0')
    setDevMode(v)
    window.dispatchEvent(new CustomEvent(DEV_MODE_EVENT, { detail: v }))
    // Notify Electron main process to show/hide DevTools menu item
    window.electronAPI?.setDevMode?.(v)
  }

  return (
    <>
    <SettingsSection title={i18nT('pages.settings.developerPanel.developer_tools')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.developerPanel.developer_mode')}
          hint={i18nT('pages.settings.developerPanel.show_developer_page_in_sidebar_with_logs_system')}
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
    {/* Between the two consent gates and the desktop-only Gateway switch:
        Developer Mode and the previews are the two things a reader comes to this
        tab to flip; the local-gateway switch is rare and platform-gated. */}
    <FeaturePreviewsSection />
    {/* The crewmate feature switches, under the Crew Members preview card that
        is their one door (`CrewmatesSection.tsx`). */}
    <CrewmatesSection />
    {/* The default agent's opt-in MCP sets (dashboard control, gateway debug):
        server-side switches, like Crewmates above (`DefaultMcpGrantsSection.tsx`). */}
    <DefaultMcpGrantsSection />
    {localGatewaySupported && (
      <SettingsSection title={i18nT('pages.settings.developerPanel.gateway')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.developerPanel.run_a_local_gateway')}
            description={i18nT('pages.settings.developerPanel.turn_it_off_only_when_a_gateway_already_answers')}
            checked={localGatewayEnabled}
            onChange={setLocalGatewayEnabled}
          />
          {/* "Off" has a prerequisite: this app's port must reach a crew saved
              for it (Set Remote Host…), through the app-held tunnel or one the
              user runs. A hand-run tunnel with no saved crew is refused as a
              foreign holder (#3810). The guide's SSH-tunnel section covers it. */}
          <a
            href={LOCAL_GATEWAY_TUNNEL_GUIDE}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline py-1"
          >
            {i18nT('pages.settings.developerPanel.how_to_set_up_an_ssh_tunnel')}
            <ExternalLink size={13} className="lucide-inline" />
          </a>
        </SettingsCard>
      </SettingsSection>
    )}
    </>
  )
}
