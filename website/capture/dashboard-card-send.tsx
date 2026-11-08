/**
 * Visual evidence for the one-click Dashboard card send preview (issue #22367).
 *
 * WHY ISOLATED: the host-side send decision is driven by a real click inside a
 * sandboxed agent-page frame and a gateway round trip, neither of which a static
 * page can stage faithfully; that path is proven by the backend and
 * `CrewDynamicDashboard` tests instead. What a reader CAN see are the two
 * user-visible surfaces this change adds, and they are drawn here with the REAL
 * components and the REAL i18n copy:
 *   - the per-crewmate Profile switch ("Send replies in one click"), a PREVIEW
 *     that is OFF by default, in both states (the real `RowGroup` / `RowStatic` /
 *     `Toggle` and the real hint string);
 *   - the error notice the host shows above the page when a card send is refused
 *     or fails and the reply is filled into the chat box instead (the real
 *     `ErrorNotice` and the real `dashboard_send_failed` string).
 *
 *   ?theme=dark|light&state=off|on|error
 */
import { createRoot } from 'react-dom/client'
import { SendHorizontal } from 'lucide-react'

import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import { Toggle } from '../src/components/ui'
import ErrorNotice from '../src/components/ErrorNotice'
import { RowGroup, RowStatic } from '../src/pages/members/profileRow'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const state = params.get('state') || 'off'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NAME = 'oncall'

function Fixture() {
  const on = state === 'on'
  return (
    <div style={{ maxWidth: 520, margin: '24px auto', padding: 16 }} data-testid="card-send-fixture">
      {state === 'error' && (
        <ErrorNotice
          message={i18nT('pages.membersPage.dashboard_send_failed')}
          className="shrink-0 m-2"
          testId="crew-dashboard-send-error"
        />
      )}
      <RowGroup caption={i18nT('pages.membersPage.profile_dashboard_card_send_hint', { name: NAME })}>
        <RowStatic
          icon={<SendHorizontal size={16} />}
          tone="accent"
          label={i18nT('pages.membersPage.profile_dashboard_card_send_switch')}
          testId="crew-profile-dashboard-card-send"
        >
          <Toggle
            checked={on}
            onChange={() => {}}
            label={i18nT('pages.membersPage.profile_dashboard_card_send_switch')}
          />
        </RowStatic>
      </RowGroup>
    </div>
  )
}

await initI18n()
createRoot(document.getElementById('root')!).render(<Fixture />)
