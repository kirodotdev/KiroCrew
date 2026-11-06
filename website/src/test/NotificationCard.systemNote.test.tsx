/**
 * A note the gateway wrote names its dashboard copy (`i18n.id`), so the bell
 * reads in the dashboard's language. A note whose id the dashboard holds no copy
 * for shows the English it was stored with.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import NotificationCard from '../components/notifications/NotificationCard'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'
import type { Notification } from '../types'

const note = (over: Partial<Notification> = {}): Notification => ({
  kind: 'agent',
  ts: '2026-11-06T10:00:00Z',
  title: 'Conversation paused: turn limit',
  body: 'A discord conversation reached its turn ceiling and is paused. Reset it from the dashboard to continue.',
  i18n: { id: 'turn_limit', params: { surface: 'discord' } },
  ...over,
})

describe('a gateway-written note', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  // Every id a gateway producer stamps today.
  it.each(['turn_limit', 'default_model_applied', 'default_model_failed'] as const)('renders the %s copy in the dashboard language', async (id) => {
    await i18next.changeLanguage('zh-CN')
    renderWithProviders(<NotificationCard n={note({ i18n: { id, params: { surface: 'discord' } } })} />)
    const copy = zh.components.notifications.systemNote
    const body = copy[`${id}_body`].replace('{{surface}}', 'discord').replace('{{productName}}', 'Kiro Crew')
    expect(screen.getByText(copy[`${id}_title`])).toBeInTheDocument()
    expect(screen.getByText(body.slice(0, 140))).toBeInTheDocument()
  })

  it.each(['not_a_copy', 'constructor'])('keeps the stored English for the id %s', async (id) => {
    await i18next.changeLanguage('zh-CN')
    renderWithProviders(<NotificationCard n={note({ title: 'Build finished', body: '42 rows', i18n: { id } })} />)
    expect(screen.getByText('Build finished')).toBeInTheDocument()
    expect(screen.getByText('42 rows')).toBeInTheDocument()
  })
})
