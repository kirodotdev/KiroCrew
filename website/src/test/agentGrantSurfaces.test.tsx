/**
 * The surfaces an agent raises, and the Settings tabs, mark what a press on them grants.
 *
 * A surface where a press can be a slip (the XR page's live panels) does not pass a plain press on a control that
 * gives an agent more than it had. The whole surface is marked at its root, and only the controls that grant
 * nothing say so, so a control that is added later is guarded until someone marks it. This test walks every kind of
 * change card, every kind of quick ask and every Settings tab with the rule the gateway's guard applies
 * (`browser_harness/panel_page.py`: the nearest mark wins; with none, a trust-root region counts as granting).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

// Settings: every panel is a stub with one control, since this test is about the pane that holds them.
const panel = vi.hoisted(() => (name: string) => () => <><button data-testid="panel-control" data-panel={name} /><input data-testid="panel-field" /></>)
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: panel('overview') }))
vi.mock('../pages/settings/ImportPanel', () => ({ ImportPanel: panel('imports') }))
vi.mock('../pages/developer/AgentBackendTab', () => ({ AgentBackendTab: panel('agent') }))
vi.mock('../pages/developer/KiroSignInCard', () => ({ KiroSignInCard: panel('kiro-account') }))
vi.mock('../pages/developer/useKiroSignInOffered', () => ({ useKiroSignInOffered: () => true }))
vi.mock('../pages/developer/KiroCreditsCard', () => ({ KiroCreditsCard: () => null }))
vi.mock('../pages/overview/KiroCrewCfgTab', () => ({ AgentRunSettings: () => null }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: panel('chat') }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: panel('display') }))
vi.mock('../pages/settings/VoicePanel', () => ({ VoicePanel: panel('voice') }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: panel('notifications') }))
vi.mock('../pages/settings/ShortcutsPanel', () => ({ ShortcutsPanel: panel('shortcuts') }))
vi.mock('../pages/settings/SkillsPanel', () => ({ SkillsPanel: panel('skills') }))
vi.mock('../pages/settings/ChannelsPanel', () => ({ ChannelsPanel: panel('channels'), CHANNEL_KEYS: [] }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: panel('browser') }))
vi.mock('../pages/settings/ComputerUsePanel', () => ({ ComputerUsePanel: panel('computer-use') }))
vi.mock('../pages/settings/WebhooksPanel', () => ({ WebhooksPanel: panel('webhooks') }))
vi.mock('../pages/settings/RemoteControlCard', () => ({ RemoteControlCard: () => null }))
vi.mock('../pages/settings/RemoteCrewPanel', () => ({ RemoteCrewPanel: panel('instances') }))
vi.mock('../pages/settings/PeoplePanel', () => ({ PeoplePanel: panel('people') }))
vi.mock('../pages/settings/PrivacyPanel', () => ({ PrivacyPanel: panel('privacy') }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: panel('security') }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: panel('connections') }))
vi.mock('../pages/settings/SecretsPanel', () => ({ SecretsPanel: panel('secrets') }))
vi.mock('../pages/settings/SecretsPanelLegacy', () => ({ SecretsPanelLegacy: panel('secrets') }))
vi.mock('../pages/settings/SessionEnvironmentsCard', () => ({ SessionEnvironmentsCard: panel('session-environments') }))
vi.mock('../pages/settings/DeveloperPanel', () => ({ DeveloperPanel: panel('developer') }))
vi.mock('../pages/settings/AdvancedPanel', () => ({ AdvancedPanel: panel('advanced') }))
vi.mock('../pages/settings/AboutPanel', () => ({ AboutPanel: panel('about') }))
vi.mock('../pages/settings/SettingsSearch', () => ({ default: () => null }))
vi.mock('../components/DataHomeOwnerNotice', () => ({ DataHomeOwnerStatusNotice: () => null }))
vi.mock('../hooks/usePreviewFlag', () => ({ usePreviewFlag: () => true }))
vi.mock('../hooks/useConnectionsUi', () => ({ useConnectionsUiEnabled: () => true, useConnectionsUiPending: () => false }))
vi.mock('../pages/settings/configPendingQuery', () => ({ useConfigPendingCount: () => 0 }))
vi.mock('../hooks/sharedCrew/useSharedCrew', () => ({ useSharedCrewCapabilities: () => ({ capabilities: undefined, isError: false }) }))
vi.mock('../api/sharedCrewUi', () => ({ sharedCrewPreviewOn: () => true }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import { cleanup, render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import '../i18n/all'
import { store as reduxStore } from '../store'
import type { Card, CardKind } from '../api/cards'
import ChangeCard from '../cards/ChangeCard'
import { CARD_REGISTRY } from '../cards/cardRegistry'
import QuickAskSheet from '../pages/chat/quickAsk/QuickAskSheet'
import { readQuickAsk } from '../pages/chat/quickAsk/types'
import SettingsPage, { buildTabs } from '../pages/SettingsPage'
import { SETTINGS_TABS_THAT_GRANT_NOTHING } from '../pages/settings/grantTabs'
import RejectDropdown from '../components/RejectDropdown'
import Modal from '../components/Modal'
import { AGENT_GRANT_ATTR, AgentGrantProvider, AgentGrantRegion, GUIDE_TRUST_ROOT_ATTR } from '../guide/trustRoot'
import { installPanelGuard, removePanelGuard, type PanelGuard } from './panelGuard'

/** The guard's own walk: the nearest mark decides; with none, a trust-root region counts as granting. */
function guarded(el: Element): boolean {
  for (let n: Element | null = el; n; n = n.parentElement) {
    if (n.hasAttribute(AGENT_GRANT_ATTR)) return n.getAttribute(AGENT_GRANT_ATTR) !== 'no'
    if (n.hasAttribute(GUIDE_TRUST_ROOT_ATTR)) return true
  }
  return false
}

/** Everything in `root` a press can land on. */
const controls = (root: Element) => [...root.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], summary, [role="button"], [role="checkbox"], [role="switch"]')]

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>{node}</MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

afterEach(() => { cleanup() })

const card = (kind: CardKind, over: Partial<Card> = {}): Card => ({
  id: `c-${kind}`,
  slot_key: 'slot-A',
  kind,
  revision: 1,
  status: 'pending',
  risk: 'normal',
  title: 'A proposed change',
  changes: [
    { label: 'First', before: 'a', after: 'b' },
    { label: 'Second', before: 'c', after: 'd', detail: true },
  ],
  editable: [],
  params: { name: 'Scout', path: 'chat.verbosity', value: 'brief' },
  plan: { apply: [], undo: [] },
  created_at: 1_700_000_000,
  expires_at: 9e12,
  ...over,
} as Card)

const KINDS = Object.keys(CARD_REGISTRY) as CardKind[]
/** The controls of a change card that grant nothing, by test id. */
const CARD_PASSED = new Set(['change-card-cancel', 'change-card-dismiss', 'change-card-details'])

describe('a change card is a grant surface', () => {
  it('walks the registry itself, so a kind that is added is covered', () => {
    expect(KINDS.length).toBeGreaterThanOrEqual(22)
  })

  it.each(KINDS)('%s: Apply is guarded, Cancel is passed, and nothing else is passed', kind => {
    wrap(<ChangeCard card={card(kind)} store={() => {}} refresh={() => {}} remove={() => {}} />)
    const root = screen.getByTestId('change-card')
    expect(root).toHaveAttribute(AGENT_GRANT_ATTR, 'yes')
    expect(guarded(screen.getByTestId('change-card-apply'))).toBe(true)
    expect(guarded(screen.getByTestId('change-card-cancel'))).toBe(false)
    for (const el of controls(root)) {
      const passedOnPurpose = CARD_PASSED.has(el.dataset.testid ?? '') || el.tagName === 'SUMMARY'
      expect([el.dataset.testid ?? el.tagName, guarded(el)]).toEqual([el.dataset.testid ?? el.tagName, !passedOnPurpose])
    }
  })

  it.each(KINDS)('%s: a failed card passes Dismiss and guards Retry', kind => {
    wrap(<ChangeCard card={card(kind, { status: 'failed' })} store={() => {}} refresh={() => {}} remove={() => {}} />)
    expect(guarded(screen.getByTestId('change-card-apply'))).toBe(true)
    expect(guarded(screen.getByTestId('change-card-dismiss'))).toBe(false)
  })

  it('an applied card passes the open and close of its details and guards Undo', () => {
    wrap(<ChangeCard card={card('setting.change', { status: 'applied', undo_until: 9e12, plan: { apply: [], undo: [{ op: 'x' }] } } as Partial<Card>)} store={() => {}} refresh={() => {}} remove={() => {}} />)
    const root = screen.getByTestId('change-card')
    const details = within(root).getByTestId('change-card-details')
    expect(guarded(details)).toBe(false)
    for (const el of controls(root)) if (el !== details && el.tagName !== 'SUMMARY') expect(guarded(el)).toBe(true)
  })
})

const ORIGIN = 'https://accounts.example-identity-provider.com:8443'
const rect = { x: 100, y: 120, w: 200, h: 40 }
const base = { origin: ORIGIN, reason: 'to go on', frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting' }
const ASKS: Record<string, unknown> = {
  credentials: { ...base, id: 'a1', kind: 'credentials', fields: [{ role: 'username', label: 'username', rect }, { role: 'password', input: 'password', rect }] },
  text: { ...base, id: 'a2', kind: 'text', fields: [{ role: 'text', input: 'email', label: 'email', rect }] },
  otp: { ...base, id: 'a3', kind: 'otp', digits: 6, fields: [{ role: 'otp', input: 'otp', rect }] },
  human_check: { ...base, id: 'a4', kind: 'human_check', fields: [{ role: 'widget', rect }] },
  confirm: { ...base, id: 'a5', kind: 'confirm', summary: 'Book 2 tickets', button: 'Place order', fields: [{ role: 'action', rect }] },
}
/** The controls of the quick ask that grant nothing, by test id. */
const ASK_PASSED = new Set(['quick-ask-not-now', 'quick-ask-decline', 'quick-ask-take-over'])

describe('the quick ask is a grant surface', () => {
  beforeEach(() => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: false, media: q, onchange: null,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it.each(Object.keys(ASKS))('%s: an answer is guarded; Not now and Decline are passed', kind => {
    const ask = readQuickAsk(ASKS[kind])
    expect(ask).not.toBeNull()
    render(<QuickAskSheet ask={ask!} sessionKey="slot:qa" mode="bottom" onCollapse={() => {}} />)
    const sheet = screen.getByTestId('quick-ask-sheet')
    expect(sheet).toHaveAttribute(AGENT_GRANT_ATTR, 'yes')
    expect(guarded(screen.getByTestId('quick-ask-not-now'))).toBe(false)
    expect(guarded(screen.getByTestId('quick-ask-decline'))).toBe(false)
    const seen = controls(sheet)
    // Not now, Decline, and at least one control that answers.
    expect(seen.length).toBeGreaterThanOrEqual(3)
    for (const el of seen) {
      const id = el.dataset.testid ?? el.tagName
      expect([id, guarded(el)]).toEqual([id, !ASK_PASSED.has(id)])
    }
  })
})

describe('a Settings tab is guarded as a whole unless it is on the list of tabs that grant nothing', () => {
  beforeEach(() => { sessionStorage.clear() })

  it('lets through exactly the three tabs that hold no granting control', () => {
    expect([...SETTINGS_TABS_THAT_GRANT_NOTHING].sort()).toEqual(['about', 'privacy', 'shortcuts'])
  })

  const TABS = buildTabs().map(t => t.key)

  it.each(TABS)('%s', async tab => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={reduxStore}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[`/settings/${tab}`]}>
            <SettingsPage />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    const control = (await screen.findAllByTestId('panel-control'))[0]
    expect(control.dataset.panel).toBe(tab)
    expect(guarded(control)).toBe(!SETTINGS_TABS_THAT_GRANT_NOTHING.has(tab))
    // A field of the tab holds the focus: the gateway's own reader says the same, so no text or key goes into it.
    const reader = installPanelGuard()
    try {
      screen.getAllByTestId('panel-field')[0].focus()
      expect(reader.focus()).toMatchObject({ editable: true, guarded: !SETTINGS_TABS_THAT_GRANT_NOTHING.has(tab) })
    } finally { removePanelGuard() }
    // The pane carries the mark itself, so the tab's content keeps the pane as its parent (its spacing rules).
    const pane = screen.getByTestId('side-panel-pane')
    expect(pane.getAttribute(AGENT_GRANT_ATTR)).toBe(SETTINGS_TABS_THAT_GRANT_NOTHING.has(tab) ? null : 'yes')
    // The list of tabs itself is never guarded: going from tab to tab grants nothing.
    const rail = screen.getAllByRole('button').filter(b => !b.dataset.testid)
    expect(rail.length).toBeGreaterThan(0)
    for (const b of rail) expect(guarded(b)).toBe(false)
  })
})

describe('what a grant surface opens through a portal', () => {
  it('a menu that carries its own `no` keeps it inside a surface that says `yes`', () => {
    render(<AgentGrantProvider><RejectDropdown onAction={() => {}} /></AgentGrantProvider>)
    const trigger = screen.getByRole('button', { name: /Reject/ })
    expect(trigger).toHaveAttribute(AGENT_GRANT_ATTR, 'no')
    fireEvent.click(trigger)
    const menu = screen.getByRole('menu')
    const items = within(menu).getAllByRole('menuitem')
    expect(items.length).toBeGreaterThan(0)
    for (const item of items) expect(guarded(item)).toBe(false)
  })

  it('a modal carries the surface\'s mark, and its own close button is passed', () => {
    render(<AgentGrantRegion><Modal open onClose={() => {}} title="A dialog"><button data-testid="inside" /></Modal></AgentGrantRegion>)
    expect(guarded(screen.getByTestId('inside'))).toBe(true)
    expect(guarded(screen.getByRole('button', { name: 'Close' }))).toBe(false)
  })

  it('a modal outside any surface carries no mark', () => {
    render(<Modal open onClose={() => {}} title="A dialog"><button data-testid="inside" /></Modal>)
    expect(guarded(screen.getByTestId('inside'))).toBe(false)
    expect(screen.getByRole('button', { name: 'Close' })).not.toHaveAttribute(AGENT_GRANT_ATTR)
  })

  it('a surface that is switched off carries nothing onto a modal, whatever stands above it', () => {
    render(<AgentGrantProvider><AgentGrantProvider on={false}><Modal open onClose={() => {}} title="A dialog"><button data-testid="inside" /></Modal></AgentGrantProvider></AgentGrantProvider>)
    expect(guarded(screen.getByTestId('inside'))).toBe(false)
  })
})

/**
 * Text and keys go to whatever holds the page's focus, so the gateway asks its reader in the page (`focus`) whether
 * the focused element is guarded, and sends neither when it is (`panel_hub._focus_holds_back`). These tests run that
 * reader, from the gateway's own source, on the real surfaces: a field inside a surface must read as guarded, or
 * what a person types would land in a form that an agent raised.
 */
describe('the gateway\'s focus reader on the real surfaces', () => {
  let reader: PanelGuard
  beforeEach(() => {
    reader = installPanelGuard()
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: false, media: q, onchange: null,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
    }))
  })
  afterEach(() => { removePanelGuard(); vi.unstubAllGlobals() })

  const focusOn = (el: HTMLElement) => { el.focus(); expect(document.activeElement).toBe(el); return reader.focus() }

  it.each(Object.keys(ASKS))('a quick ask (%s): every field and the sheet itself read as guarded; Decline does not', kind => {
    render(<QuickAskSheet ask={readQuickAsk(ASKS[kind])!} sessionKey="slot:qa" mode="bottom" onCollapse={() => {}} />)
    const sheet = screen.getByTestId('quick-ask-sheet')
    // The sheet takes the focus by itself when it opens: with no press at all, typing must not land in it.
    expect(focusOn(sheet).guarded).toBe(true)
    // Every field, the ones that only show a value included.
    const fields = [...sheet.querySelectorAll<HTMLInputElement>('input, textarea')]
    for (const field of fields) expect(focusOn(field).guarded).toBe(true)
    if (kind === 'credentials' || kind === 'text' || kind === 'otp') expect(fields.filter(f => !f.readOnly && focusOn(f).editable).length).toBeGreaterThan(0)
    expect(focusOn(screen.getByTestId('quick-ask-decline')).guarded).toBe(false)
    expect(focusOn(screen.getByTestId('quick-ask-not-now')).guarded).toBe(false)
  })

  it('a change card\'s secret field reads as guarded; Tab from Cancel into it changes the answer', () => {
    const plan = { apply: [{ method: 'POST', path: '/api/secrets', body: {}, fill: [{ field: 'value', source: 'user' }] }], undo: [] }
    wrap(<ChangeCard card={card('secret.save', { plan } as Partial<Card>)} store={() => {}} refresh={() => {}} remove={() => {}} />)
    // On Cancel, which grants nothing, a key is passed.
    expect(focusOn(screen.getByTestId('change-card-cancel')).guarded).toBe(false)
    // The next stop is inside the surface: text and keys stop there.
    const secret = screen.getByTestId('change-card-input-value')
    expect(focusOn(secret)).toMatchObject({ editable: true, kind: 'password', guarded: true })
    // Apply takes the focus once the field holds a value, and reads as guarded too.
    fireEvent.change(secret, { target: { value: 'a value' } })
    expect(focusOn(screen.getByTestId('change-card-apply')).guarded).toBe(true)
  })

  it('a widget frame reads by where it stands: guarded inside a surface, not guarded outside', () => {
    render(<><iframe data-testid="widget" sandbox="allow-scripts" title="w" /><AgentGrantRegion><iframe data-testid="inside" sandbox="allow-scripts" title="i" /></AgentGrantRegion></>)
    expect(focusOn(screen.getByTestId('widget'))).toMatchObject({ kind: 'frame', guarded: false })
    expect(focusOn(screen.getByTestId('inside'))).toMatchObject({ kind: 'frame', guarded: true })
  })

  it('with the focus on nothing it reads as not guarded', () => {
    render(<AgentGrantRegion><button data-testid="inside" /></AgentGrantRegion>)
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(reader.focus()).toEqual({ editable: false, kind: '', rect: null, guarded: false })
  })
})
