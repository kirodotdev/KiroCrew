import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstanceTabBar, { setCrewPins } from '../components/InstanceTabBar'
import {
  CREW_ICONS_PREF_KEY,
  CREW_ICON_KEYS,
  CrewIconInBadge,
  __reloadCrewIconsForTests,
  assignCrewIcons,
  crewIconOf,
  ensureCrewIcons,
  markShape,
  parseCrewIcons,
  setCrewIcon,
  type CrewIconKey,
} from '../components/crewIcons'
import { CrewIconPicker } from '../components/crewIconPicker'
import { parseHostModel } from '../components/EmbeddedHostBridge'
import { CrewLetterBadge } from '../components/crewStateMark'
import type { InstanceView, SsoStatus } from '../api/client'

vi.mock('../api/client', () => ({
  api: { listInstances: vi.fn(), connectInstance: vi.fn() },
}))
import { api } from '../api/client'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

const crew = (id: string, name: string): InstanceView => ({
  id,
  name,
  ssh_host: `${id}-alias`,
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  connection_method: 'ssh',
  ssm_target: '',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  was_connected: true,
  status: { instance_id: id, state: 'connected', local_port: 7778, remote_port: 7777 },
})

const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const listResp = (instances: InstanceView[]) => ({ active: true, instances, warm_set_cap: 5, sso: okSso })

/** A deterministic stand-in for Math.random: always the first candidate. */
const first = () => 0

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  __reloadCrewIconsForTests()
  setCrewPins([])
})

describe('assignCrewIcons', () => {
  it('gives a new crew an icon none of the listed crews uses', () => {
    const inUse: Record<string, CrewIconKey> = {}
    CREW_ICON_KEYS.slice(0, CREW_ICON_KEYS.length - 1).forEach((k, i) => (inUse[`c${i}`] = k))
    const ids = [...Object.keys(inUse), 'new']
    // Every random draw must land on the one free icon.
    for (const r of [0, 0.5, 0.999]) {
      expect(assignCrewIcons(inUse, ids, () => r).new).toBe(CREW_ICON_KEYS[CREW_ICON_KEYS.length - 1])
    }
  })

  it('assigns several new crews distinct icons in one pass', () => {
    const ids = CREW_ICON_KEYS.map((_, i) => `c${i}`)
    const next = assignCrewIcons({}, ids, first)
    expect(new Set(ids.map(id => next[id])).size).toBe(CREW_ICON_KEYS.length)
  })

  it('reuses only the least used icons once every icon is taken', () => {
    const ids = [...CREW_ICON_KEYS.map((_, i) => `c${i}`), 'extra1', 'extra2']
    const next = assignCrewIcons({}, ids, first)
    expect(next.extra1).not.toBe(next.extra2)
  })

  it('frees the icon of a crew that is no longer listed', () => {
    const current = { gone: 'star' as const }
    expect(assignCrewIcons(current, ['new'], first).new).toBe('star')
  })

  it('returns the same map when every listed crew has an icon', () => {
    const current = { a: 'moon' as const }
    expect(assignCrewIcons(current, ['a'], first)).toBe(current)
  })

  it('gives a crew whose id names an Object.prototype member its own icon', () => {
    // A crew id is a slug of the name, so a crew called "Constructor" has this id.
    expect(crewIconOf({}, 'constructor')).toBeUndefined()
    const next = assignCrewIcons({}, ['constructor'], first)
    expect(crewIconOf(next, 'constructor')).toBe(CREW_ICON_KEYS[0])
  })

  it('drops stored entries that name no known icon', () => {
    expect(parseCrewIcons(JSON.stringify({ a: 'moon', b: 'not-an-icon', c: 7 }))).toEqual({ a: 'moon' })
    expect(parseCrewIcons('{broken')).toEqual({})
    expect(parseCrewIcons('["moon"]')).toEqual({})
  })
})

describe('markShape', () => {
  it('gives live, error and the other states three different shapes', () => {
    expect(markShape('connected')).toBe('dot')
    expect(markShape('error')).toBe('diamond')
    expect(new Set(['stopped', 'disconnected', undefined].map(markShape))).toEqual(new Set(['ring']))
  })
})

describe('the phone badge with an icon', () => {
  it('holds the icon in place of letters and keeps the outline that names the state', () => {
    render(
      <CrewLetterBadge state="error">
        <CrewIconInBadge icon="cat" />
      </CrewLetterBadge>,
    )
    const badge = screen.getByTestId('crew-letter-badge')
    expect(badge.querySelector('[data-crew-icon="cat"]')).not.toBeNull()
    expect(badge).toHaveTextContent('')
    expect(badge).toHaveClass('border', 'border-dashed')
  })
})

describe('crew icon store', () => {
  it('keeps the assignment across a reload', () => {
    ensureCrewIcons(['a', 'b'])
    const raw = localStorage.getItem(CREW_ICONS_PREF_KEY)
    const assigned = parseCrewIcons(raw)
    expect(Object.keys(assigned).sort()).toEqual(['a', 'b'])

    __reloadCrewIconsForTests()
    // A fresh draw would pick again; an assignment read back from storage must not.
    ensureCrewIcons(['a', 'b'], () => 0.999)
    expect(localStorage.getItem(CREW_ICONS_PREF_KEY)).toBe(raw)
    expect(parseCrewIcons(localStorage.getItem(CREW_ICONS_PREF_KEY))).toEqual(assigned)
  })
})

/** What the browser delivers to this tab when another tab of the origin writes the map. */
function otherTabWrites(map: Record<string, CrewIconKey>) {
  const newValue = JSON.stringify(map)
  localStorage.setItem(CREW_ICONS_PREF_KEY, newValue)
  window.dispatchEvent(new StorageEvent('storage', { key: CREW_ICONS_PREF_KEY, newValue, storageArea: localStorage }))
}

/** Make every write of the icon map throw, as a full or locked storage does; reads still work. */
function refuseIconWrites() {
  const realSet = Storage.prototype.setItem
  return vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (key === CREW_ICONS_PREF_KEY) throw new Error('write refused')
    realSet.call(this, key, value)
  })
}

describe('two tabs of one dashboard', () => {
  it('keeps the pick another tab saved when this tab assigns a new crew', () => {
    ensureCrewIcons(['a'], first)
    otherTabWrites({ a: 'moon' })
    ensureCrewIcons(['a', 'b'])
    const stored = parseCrewIcons(localStorage.getItem(CREW_ICONS_PREF_KEY))
    expect(stored.a).toBe('moon')
    expect(stored.b).toBeTruthy()
    expect(stored.b).not.toBe('moon')
  })

  it('keeps the pick another tab saved before its storage event reaches this tab', () => {
    ensureCrewIcons(['a', 'b'], first)
    // The other tab's write is in storage; its event has not been delivered yet.
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ ...parseCrewIcons(localStorage.getItem(CREW_ICONS_PREF_KEY)), a: 'moon' }))
    setCrewIcon('b', 'cat')
    ensureCrewIcons(['a', 'b', 'c'], first)
    const stored = parseCrewIcons(localStorage.getItem(CREW_ICONS_PREF_KEY))
    expect(stored.a).toBe('moon')
    expect(stored.b).toBe('cat')
    expect(stored.c).toBeTruthy()
    expect(['moon', 'cat']).not.toContain(stored.c)
  })

  it('draws the stored map when a late storage event carries an older one', () => {
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ a: 'dog' }))
    window.dispatchEvent(
      new StorageEvent('storage', { key: CREW_ICONS_PREF_KEY, newValue: JSON.stringify({ a: 'cat' }), storageArea: localStorage }),
    )
    render(<CrewIconPicker id="a" name="Alpha" />)
    expect(screen.getByTestId('crew-icon-picker-a').getAttribute('aria-label')).toMatch(/Dog/)
  })

  it('keeps a new crew\'s icon when storage reads work but the write is refused', () => {
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ x: 'star' }))
    __reloadCrewIconsForTests()
    const refuse = refuseIconWrites()
    try {
      // `x` holds the star, so the first free icon is the moon.
      ensureCrewIcons(['x', 'a'], first)
      // Later crew-list reads draw differently; with the write refused, the
      // icon must still not move.
      ensureCrewIcons(['x', 'a'], () => 0.999)
      ensureCrewIcons(['x', 'a'], () => 0.5)
      expect(localStorage.getItem(CREW_ICONS_PREF_KEY)).toBe(JSON.stringify({ x: 'star' }))
      render(<CrewIconPicker id="a" name="Alpha" />)
      expect(screen.getByTestId('crew-icon-picker-a').getAttribute('aria-label')).toMatch(/Moon/)
    } finally {
      refuse.mockRestore()
    }
  })

  it('keeps a refused pick when the other tab\'s queued storage event arrives after it', () => {
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ x: 'star' }))
    __reloadCrewIconsForTests()
    // The other tab saves the moon; its event has not reached this tab yet.
    const moon = JSON.stringify({ x: 'moon' })
    localStorage.setItem(CREW_ICONS_PREF_KEY, moon)
    const refuse = refuseIconWrites()
    try {
      render(<CrewIconPicker id="x" name="Ex" />)
      act(() => setCrewIcon('x', 'cat'))
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: CREW_ICONS_PREF_KEY, newValue: moon, storageArea: localStorage }))
      })
      expect(screen.getByTestId('crew-icon-picker-x').getAttribute('aria-label')).toMatch(/Cat/)
    } finally {
      refuse.mockRestore()
    }
  })

  it('keeps a pick in this tab when storage reads work but the write is refused', () => {
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ x: 'star' }))
    __reloadCrewIconsForTests()
    const refuse = refuseIconWrites()
    try {
      render(<CrewIconPicker id="x" name="Ex" />)
      const picker = () => screen.getByTestId('crew-icon-picker-x').getAttribute('aria-label')
      act(() => setCrewIcon('x', 'cat'))
      // Assigning another crew must not put the star that storage still holds back over the pick.
      act(() => ensureCrewIcons(['x', 'a'], first))
      expect(picker()).toMatch(/Cat/)
      act(() => setCrewIcon('x', 'star'))
      expect(picker()).toMatch(/Star/)
    } finally {
      refuse.mockRestore()
    }
  })

  it.each([
    ['with no write adopted in between', false],
    ['after adopting an earlier write of that tab', true],
  ])('keeps another tab\'s queued pick once storage takes this tab\'s writes again, %s', (_when, adoptFirst) => {
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ a: 'star' }))
    __reloadCrewIconsForTests()
    const refuse = refuseIconWrites()
    ensureCrewIcons(['a', 'b'], first)
    refuse.mockRestore()
    if (adoptFirst) otherTabWrites({ a: 'fish' })
    // The other tab picks for `a`; its event has not reached this tab yet.
    localStorage.setItem(CREW_ICONS_PREF_KEY, JSON.stringify({ a: 'cat' }))
    setCrewIcon('b', 'dog')
    const stored = parseCrewIcons(localStorage.getItem(CREW_ICONS_PREF_KEY))
    expect(stored.a).toBe('cat')
    expect(stored.b).toBe('dog')
  })

  it('draws the icon another tab assigned, without a write of its own', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([crew('a', 'Alpha')]))
    otherTabWrites({ a: 'cat' })
    const store = createTestStore({ instances: { warm: {}, activeId: 'a', mru: ['a'], unread: {} } })
    renderWithProviders(<InstanceTabBar />, { store })
    const chip = await screen.findByRole('button', { name: /^Alpha/ })
    expect(chip.querySelector('[data-crew-icon]')?.getAttribute('data-crew-icon')).toBe('cat')
    otherTabWrites({ a: 'dog' })
    await waitFor(() => expect(chip.querySelector('[data-crew-icon]')?.getAttribute('data-crew-icon')).toBe('dog'))
  })
})

describe('crew icons in the switcher', () => {
  it('draws each remote crew its own icon and keeps the house for Local', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([crew('a', 'Alpha'), crew('b', 'Beta')]))
    const store = createTestStore({ instances: { warm: {}, activeId: 'a', mru: ['a'], unread: {} } })
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />, { store })

    await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
    const local = await screen.findByRole('menuitemradio', { name: /Local/i })
    expect(local.querySelector('svg.lucide-house')).not.toBeNull()
    expect(local.querySelector('[data-crew-icon]')).toBeNull()

    const iconOf = (name: RegExp) =>
      screen.getByRole('menuitemradio', { name }).querySelector('[data-crew-icon]')?.getAttribute('data-crew-icon')
    const a = iconOf(/Alpha/)
    const b = iconOf(/Beta/)
    expect(a).toBeTruthy()
    expect(b).toBeTruthy()
    expect(a).not.toBe(b)
    // The state stays on the icon as its mark.
    const mark = within(screen.getByRole('menuitemradio', { name: /Alpha/ })).getByTestId('crew-state-mark')
    expect(mark.getAttribute('data-state')).toBe('connected')
    expect(mark.getAttribute('data-shape')).toBe('dot')
  })

  it('shows the icon picked in Settings on the crew chip', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([crew('a', 'Alpha')]))
    setCrewIcon('a', 'star')
    const store = createTestStore({ instances: { warm: {}, activeId: 'a', mru: ['a'], unread: {} } })
    const u = userEvent.setup()
    renderWithProviders(
      <>
        <InstanceTabBar />
        <CrewIconPicker id="a" name="Alpha" />
      </>,
      { store },
    )
    const chip = await screen.findByRole('button', { name: /^Alpha/ })
    expect(chip.querySelector('[data-crew-icon]')?.getAttribute('data-crew-icon')).toBe('star')

    await u.click(screen.getByTestId('crew-icon-picker-a'))
    await u.click(await screen.findByRole('menuitemradio', { name: 'Moon' }))

    await waitFor(() =>
      expect(chip.querySelector('[data-crew-icon]')?.getAttribute('data-crew-icon')).toBe('moon'),
    )
    expect(parseCrewIcons(localStorage.getItem(CREW_ICONS_PREF_KEY))).toEqual({ a: 'moon' })
  })
})

describe('relayed crew icons', () => {
  it('passes a known icon word into the pane and drops anything else', () => {
    const parsed = parseHostModel({
      type: 'mc-host-model',
      v: 1,
      activeId: null,
      tabs: [
        { id: 'a', name: 'Alpha', sshHost: 'h', unread: 0, icon: 'cat' },
        { id: 'b', name: 'Beta', sshHost: 'h', unread: 0, icon: 'constructor' },
        { id: 'c', name: 'Gamma', sshHost: 'h', unread: 0 },
      ],
    })
    expect(parsed?.tabs.map(t => t.icon)).toEqual(['cat', undefined, undefined])
  })
})
