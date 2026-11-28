/**
 * Test: a built-in rail badge's tooltip reads in the dashboard's language.
 *
 * Surfaces register at module load, before a language is known, so a plain
 * `badgeLabel` / `activityLabel` string is English forever: the Sessions, Bots,
 * Notifications and Task Runner badges showed "unread conversations",
 * "approvals needed" and the like in every language. Each now names a catalog
 * key that the rail resolves when it renders.
 *
 * The screen-reader name carries the count, so it comes from a plural catalog
 * string resolved with that count: a number glued in front of the count-free
 * label read "1 unread conversations", and the wrong noun form in Russian.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { act, screen } from '@testing-library/react'
import { i18next } from '../i18n/index'
import '../i18n/all'
import { i18nT } from '../i18n/t'
import '../surfaces/builtins'
import { getBuiltinSurface } from '../surfaces/registry'
import { NavBadge } from '../App'
import { markSlotUnread } from '../store/dashboardSlice'
import { sseSubagentQueued } from '../store/chatSlice'
import type { ChatSlot } from '../types'
import { createTestStore, renderWithProviders } from './helpers'

/** Renders `navId`'s rail badge with `count` items through the path that feeds
 *  it in the app: unread slots or the `appBadges` fallback. */
function renderBadge(navId: string, count: number) {
  const keys = Array.from({ length: count }, (_, i) => `${navId}-${i}`)
  // A member thread counts only while its slot is listed with the member surface.
  const store = createTestStore(navId === 'members'
    ? { dashboard: { ...createTestStore().getState().dashboard, slots: keys.map(key => ({ key, surface: 'member' }) as ChatSlot) } }
    : undefined)
  for (const slot of keys) {
    if (navId === 'chat' || navId === 'members') store.dispatch(markSlotUnread({ slot, ts: '2026-01-01T00:00:05Z' }))
  }
  const appName = navId.startsWith('app-') ? navId.slice(4) : navId
  renderWithProviders(<NavBadge navId={navId} collapsed={false} appBadges={{ [appName]: count }} />, { store })
  return screen.getByText(String(count))
}

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('built-in rail badge labels', () => {
  it.each(['chat', 'members', 'notifications', 'projects'])('%s names its badge label by a catalog key', navId => {
    const key = getBuiltinSurface(navId)?.badgeLabelKey
    expect(key).toBeTruthy()
    // The English value is the fallback string the surface still carries.
    expect(i18nT(key!)).toBe(getBuiltinSurface(navId)?.badgeLabel)
  })

  it('leaves the Sessions activity label to the rail fallback, which is a catalog key', () => {
    // A fixed `activityLabel` string would be English; the fallback resolves
    // `app.subagents_in_flight_fallback` when the rail renders.
    expect(getBuiltinSurface('chat')?.activitySelector).toBeTypeOf('function')
    expect(getBuiltinSurface('chat')?.activityLabel).toBeUndefined()
  })

  it('shows the tooltip in German', async () => {
    await i18next.changeLanguage('de')
    renderWithProviders(<NavBadge navId="projects" collapsed={false} appBadges={{ projects: 2 }} />, { store: createTestStore() })
    const pill = await screen.findByText('2')
    expect(pill).toHaveAttribute('title', 'ausstehende Freigaben')
  })
})

describe('rail badge accessible names', () => {
  it.each([
    ['chat', 'unread conversation', 'unread conversations'],
    ['members', 'unread member thread', 'unread member threads'],
    ['projects', 'approval needed', 'approvals needed'],
    ['capabilities', 'server waiting for approval', 'servers waiting for approval'],
  ])('%s reads the plural form for its count, and keeps a count-free title', (navId, one, other) => {
    const pill = renderBadge(navId, 1)
    expect(pill).toHaveAttribute('aria-label', `1 ${one}`)
    expect(pill).toHaveAttribute('title', other)
    renderBadge(navId, 2)
    expect(screen.getByText('2')).toHaveAttribute('aria-label', `2 ${other}`)
    expect(screen.getByText('2')).toHaveAttribute('title', other)
  })

  it('names an app row with no surface label by its own plural string', () => {
    expect(renderBadge('app-demo', 1)).toHaveAttribute('aria-label', '1 update')
    expect(renderBadge('app-demo', 2)).toHaveAttribute('aria-label', '2 updates')
  })

  it('names the Sessions activity readout by its plural string', () => {
    const store = createTestStore()
    renderWithProviders(<NavBadge navId="chat" collapsed={false} appBadges={{}} />, { store })
    act(() => { store.dispatch(sseSubagentQueued({ slot: 'background', queued: 1 })) })
    expect(screen.getByRole('status', { name: '1 subagent in flight' })).toHaveAttribute('title', 'subagents in flight')
  })

  it('puts the count inside the Russian one, few and many forms', async () => {
    await i18next.changeLanguage('ru')
    expect(renderBadge('chat', 1)).toHaveAttribute('aria-label', '1 непрочитанный разговор')
    expect(renderBadge('chat', 2)).toHaveAttribute('aria-label', '2 непрочитанных разговора')
    const pill = renderBadge('chat', 5)
    expect(pill).toHaveAttribute('aria-label', '5 непрочитанных разговоров')
    expect(pill).toHaveAttribute('title', 'непрочитанные разговоры')
  })
})
