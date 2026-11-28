/* The crew switcher's chips in the phone form: icon-only. A remote crew wears
 * its icon in the badge's disc, whose outline names the state; a chip with no
 * icon keeps its letters there. The desktop form keeps the name. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstanceTabBar, { setCrewPins, setStableOrder } from '../components/InstanceTabBar'
import { __reloadCrewIconsForTests } from '../components/crewIcons'
import type { HostModel } from '../store/instancesSlice'
import type { InstanceView, SsoStatus } from '../api/client'

vi.mock('../api/client', () => ({ api: { listInstances: vi.fn(), connectInstance: vi.fn() } }))
import { api } from '../api/client'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'
let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const crew = (id: string, name: string, state = 'connected'): InstanceView => ({
  id,
  name,
  ssh_host: `${id}-alias`,
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  status: { instance_id: id, state, local_port: 7778, remote_port: 7777 },
})
const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const CREWS = [crew('stg', 'staging'), crew('sbx', 'sandbox', 'error'), crew('dev', 'devdesk')]

function renderBar(activeId: string | null, crews: InstanceView[] = CREWS) {
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: crews, warm_set_cap: 5, sso: okSso })
  const store = createTestStore({ instances: { warm: {}, activeId, mru: activeId ? [activeId] : [], unread: {} } })
  return renderWithProviders(<InstanceTabBar variant="inline" />, { store })
}

async function activeChip(container: HTMLElement) {
  await screen.findByRole('button', { name: /Switch crew/i })
  await waitFor(() => expect(container.querySelector('.tb-crew-active-chip')).not.toBeNull())
  return container.querySelector<HTMLElement>('.tb-crew-active-chip')!
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  __reloadCrewIconsForTests()
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
  setCrewPins([])
  setStableOrder(false)
  mobile = true
})

describe('InstanceTabBar on a phone', () => {
  it('draws a remote crew as its icon in the badge disc, and the name only in the accessible name', async () => {
    const { container } = renderBar('stg')
    const chip = await activeChip(container)
    const badge = within(chip).getByTestId('crew-letter-badge')
    expect(badge.querySelector('[data-crew-icon]')?.getAttribute('data-crew-icon')).toBeTruthy()
    expect(badge).toHaveAttribute('data-state', 'connected')
    expect(badge).toHaveTextContent('')
    // A live badge is the filled disc alone: no state mark.
    expect(within(chip).queryByTestId('crew-badge-mark')).toBeNull()
    expect(chip).not.toHaveTextContent('staging')
    expect(chip.getAttribute('aria-label')).toContain('staging')
  })

  it('keeps pinned chips icon-only, with no state word, and the badge outlined for a tunnel that is down', async () => {
    setCrewPins(['sbx', 'dev'])
    const { container } = renderBar('stg')
    await activeChip(container)
    const row = await screen.findByTestId('crew-chip-row')
    const badges = within(row).getAllByTestId('crew-letter-badge')
    expect(badges.map(b => b.getAttribute('data-state'))).toEqual(['error', 'connected'])
    expect(badges.every(b => b.querySelector('[data-crew-icon]'))).toBe(true)
    expect(badges[0]).toHaveClass('border', 'border-dashed')
    expect(row).not.toHaveTextContent(/sandbox|devdesk/)
    // The state word "Tunnel error" stays in the accessible name and the menu.
    expect(row).not.toHaveTextContent(/error/i)
  })

  it('tells error, connecting and stopped apart by shape and mark, with the state word in the name and the menu', async () => {
    // English catalog words: `tunnel_error`, `connecting` and `stopped` under
    // components.instanceTabBar.
    const crews = [
      crew('stg', 'staging'),
      crew('alp', 'alpha', 'error'),
      crew('kil', 'kilo', 'connecting'),
      crew('mik', 'mike', 'stopped'),
    ]
    setCrewPins(['alp', 'kil', 'mik'])
    const { container } = renderBar('stg', crews)
    await activeChip(container)
    const row = await screen.findByTestId('crew-chip-row')
    const chip = (name: string) => within(row).getByRole('button', { name: new RegExp(`^${name}\\b`) })

    const error = chip('alpha')
    expect(error.getAttribute('aria-label')).toMatch(/tunnel error/i)
    const errorBadge = within(error).getByTestId('crew-letter-badge')
    expect(errorBadge).toHaveAttribute('data-state', 'error')
    expect(errorBadge).toHaveClass('border', 'border-dashed')
    expect(errorBadge.querySelector('[data-crew-icon]')).not.toBeNull()
    // The `!` mark tells an error apart without the outline or the colour.
    const errorMark = within(errorBadge).getByTestId('crew-badge-mark')
    expect(errorMark).toHaveAttribute('data-mark', 'error')
    expect(errorMark).toHaveClass('lucide-circle-alert')
    expect(errorMark).toHaveAttribute('aria-hidden', 'true')
    // The mark is a drawing, not text: the badge holds no letters beside the icon.
    expect(errorBadge).toHaveTextContent('')

    // A connecting crew's chip draws the spinner in place of the badge.
    const connecting = chip('kilo')
    expect(connecting.getAttribute('aria-label')).toMatch(/connecting/i)
    expect(within(connecting).queryByTestId('crew-letter-badge')).toBeNull()
    expect(connecting.querySelector('svg.animate-spin')).not.toBeNull()

    const stopped = chip('mike')
    expect(stopped.getAttribute('aria-label')).toMatch(/stopped/i)
    const stoppedBadge = within(stopped).getByTestId('crew-letter-badge')
    expect(stoppedBadge).toHaveAttribute('data-state', 'stopped')
    expect(stoppedBadge).toHaveClass('border', 'border-solid')
    expect(stoppedBadge).not.toHaveClass('border-dashed')
    expect(stoppedBadge.querySelector('[data-crew-icon]')).not.toBeNull()
    const stoppedMark = within(stoppedBadge).getByTestId('crew-badge-mark')
    expect(stoppedMark).toHaveAttribute('data-mark', 'stopped')
    expect(stoppedMark).toHaveClass('lucide-square')
    expect(stoppedMark).toHaveAttribute('aria-hidden', 'true')
    expect(stoppedBadge).toHaveTextContent('')
    // The live crew's badge carries no mark.
    const live = container.querySelector<HTMLElement>('.tb-crew-active-chip')!
    expect(within(live).queryByTestId('crew-badge-mark')).toBeNull()

    // The menu row still names the state in visible words.
    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: /Switch crew/i }))
    const menuRow = await screen.findByRole('menuitemradio', { name: /alpha/ })
    const word = within(menuRow).getByText(/tunnel error/i)
    expect(word).not.toHaveClass('sr-only')
  })

  it('shows Local as the house alone', async () => {
    const { container } = renderBar(null)
    const chip = await activeChip(container)
    expect(chip.querySelector('svg')).not.toBeNull()
    expect(chip).not.toHaveTextContent(/Local/)
    expect(chip.getAttribute('aria-label')).toMatch(/Local/)
  })
})

describe('a phone chip with no icon', () => {
  it('keeps the letter badge, as in a pane whose host relays no icons', async () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const host: HostModel = {
      tabs: [
        { id: 'stg', name: 'staging', sshHost: 'stg-alias', state: 'connected', unread: 0 },
        { id: 'sbx', name: 'sandbox', sshHost: 'sbx-alias', state: 'error', unread: 0 },
      ],
      activeId: 'stg',
      self: null,
      macInset: false,
      winInset: false,
      linuxInset: false,
      focusMode: null,
      electron: true,
      pinnedCrews: [],
      stableOrder: false,
    }
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {}, host } })
    const { container } = renderWithProviders(<InstanceTabBar variant="inline" />, { store })
    const chip = await activeChip(container)
    // `staging` and `sandbox` share an S, so the badge takes the second letter.
    expect(within(chip).getByTestId('crew-letter-badge')).toHaveTextContent(/^St$/)
    expect(chip.querySelector('[data-crew-icon]')).toBeNull()
  })
})

describe('InstanceTabBar on a desktop', () => {
  it('keeps the name beside the icon', async () => {
    mobile = false
    const { container } = renderBar('stg')
    const chip = await activeChip(container)
    expect(chip).toHaveTextContent('staging')
    expect(chip.querySelector('[data-crew-icon]')).not.toBeNull()
    expect(within(chip).queryByTestId('crew-letter-badge')).toBeNull()
  })
})
