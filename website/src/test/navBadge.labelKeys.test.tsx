/**
 * Test: a built-in rail badge's tooltip reads in the dashboard's language.
 *
 * Surfaces register at module load, before a language is known, so a plain
 * `badgeLabel` / `activityLabel` string is English forever: the Sessions, Bots,
 * Notifications and Task Runner badges showed "unread conversations",
 * "approvals needed" and the like in every language. Each now names a catalog
 * key that the rail resolves when it renders.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { i18next } from '../i18n/index'
import '../i18n/all'
import { i18nT } from '../i18n/t'
import '../surfaces/builtins'
import { getBuiltinSurface } from '../surfaces/registry'
import { NavBadge } from '../App'
import { createTestStore, renderWithProviders } from './helpers'

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
