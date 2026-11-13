/**
 * The feed's search matches a gateway-written note by the copy it shows, so a
 * language switch with a search typed in re-runs the match in the new language.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const note: Notification = {
  kind: 'agent',
  ts: '2026-11-06T10:00:00Z',
  title: 'Default model applied',
  body: 'The saved default model is now in effect.',
  acked: false,
  i18n: { id: 'default_model_applied' },
}

describe('searching the feed across a language switch', () => {
  afterEach(async () => { await act(async () => { await i18next.changeLanguage('en') }) })

  it('matches a gateway-written note in the language it now shows', async () => {
    await i18next.changeLanguage('en')
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store })
    const title = zh.components.notifications.systemNote.default_model_applied_title
    fireEvent.change(screen.getByRole('textbox'), { target: { value: title } })
    expect(screen.queryByText(title)).toBeNull()

    await act(async () => { await i18next.changeLanguage('zh-CN') })
    expect(screen.getByText(title)).toBeInTheDocument()
  })
})
