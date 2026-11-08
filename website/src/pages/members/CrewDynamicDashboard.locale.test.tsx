import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { createConnectedTestStore, renderWithProviders } from '../../test/helpers'

/**
 * A LANGUAGE SWITCH IS A NEW QUERY KEY, so it loads with nothing cached. The page
 * already on screen must stay mounted through that load. Unmounting it for the
 * skeleton meant that when the read for the new key failed, the kept page was
 * mounted again on its spent document url, which the gateway serves only once.
 */

const memberDashboard = vi.fn()
let locale = 'en'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { memberDashboard: (...a: unknown[]) => memberDashboard(...a) } }
})

vi.mock('../../i18n/LanguageProvider', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../i18n/LanguageProvider')>()
  return { ...actual, useLanguage: () => ({ ...actual.useLanguage(), resolved: locale }) }
})

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? 'https://localhost/sandbox/doc' : null,
    failed: false,
    pending: false,
    stalled: false,
    retry: () => {},
  }),
}))

import CrewDynamicDashboard from './CrewDynamicDashboard'

const answer = {
  slug: 'atlas',
  member: 'Atlas',
  dashboard: 'page-one',
  state: 'ok',
  html: '<p>stored</p>',
  rendered_html: '<p>page</p>',
  manifest: { fields: {} },
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  read: { fields: {}, version: 1, generation: 'gen-a' },
}

const TARGET = { kind: 'member' as const, slug: 'atlas', member: 'Atlas' }

describe('a language switch whose read fails', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    locale = 'en'
  })

  it('keeps the same frame mounted the whole time, never the skeleton', async () => {
    memberDashboard.mockResolvedValue(answer)
    const ui = <CrewDynamicDashboard target={TARGET} displayName="Atlas" />
    const view = renderWithProviders(ui, { store: createConnectedTestStore() })
    await waitFor(() => {
      if (!view.container.querySelector('iframe')) throw new Error('the frame never mounted')
    })
    const frame = view.container.querySelector('iframe')

    locale = 'fr'
    memberDashboard.mockRejectedValue(new Error('503 the gateway is restarting'))
    view.rerender(ui)
    expect(view.queryByTestId('crew-dashboard-loading')).toBeNull()
    expect(view.container.querySelector('iframe')).toBe(frame)

    await waitFor(() => expect(view.getByTestId('crew-dashboard-read-error-band')).toBeTruthy())
    expect(memberDashboard).toHaveBeenLastCalledWith('atlas', 'Atlas', 'fr')
    expect(view.container.querySelector('iframe')).toBe(frame)
  })
})
