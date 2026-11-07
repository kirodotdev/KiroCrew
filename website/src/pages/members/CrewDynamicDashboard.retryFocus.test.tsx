// @vitest-environment jsdom
/**
 * A RETRY KEEPS ITS FOCUS WHILE IT RUNS.
 *
 * Each band on a crewmate's Dashboard tab stays mounted while its Retry re-asks.
 * A native `disabled` on the button the reader just pressed blurs it to `<body>`,
 * so a keyboard user lost their place in the band before the answer came. The
 * button is held with `aria-disabled` instead, and a second press is refused.
 */
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard from './CrewDynamicDashboard'

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

const sandbox = vi.hoisted(() => ({ failed: false, retries: 0 }))

vi.mock('../../hooks/useSandboxDoc', () => ({
  // A real hook, so a press flips `pending` the way the real one does.
  useSandboxDoc: (srcdoc: string | null) => {
    const [pending, setPending] = useState(false)
    return {
      url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null,
      pending,
      failed: Boolean(srcdoc) && sandbox.failed,
      stalled: false,
      retry: () => {
        sandbox.retries += 1
        setPending(true)
      },
    }
  },
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest
const TARGET = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
const DASHBOARD_KEY = ['member-dashboard', 'atlas']

const PAGE = {
  instance_version: 1,
  template: { id: 'atlas-tree', version: 1 },
  html: '<!doctype html><p>stored</p>',
  rendered_html: '<!doctype html><p>page</p>',
  manifest: MANIFEST,
  state: 'live' as const,
}

const mount = () => renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" />)

describe('a dashboard band Retry', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    sandbox.failed = false
    sandbox.retries = 0
  })
  afterEach(() => {
    sandbox.failed = false
  })

  it('keeps focus on the read-error Retry while the re-read is out', async () => {
    let settle: (value: typeof PAGE) => void = () => {}
    const read = vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(PAGE)
      .mockRejectedValueOnce(new Error('down'))
      .mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    })
    const retry = await screen.findByTestId('crew-dashboard-read-error-retry')

    retry.focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(read).toHaveBeenCalledTimes(3))
    expect(retry.getAttribute('aria-disabled')).toBe('true')
    expect(retry.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(retry)

    await userEvent.keyboard('{Enter}')
    expect(read).toHaveBeenCalledTimes(3)

    await act(async () => settle(PAGE))
    await waitFor(() => expect(screen.queryByTestId('crew-dashboard-read-error-band')).toBeNull())
  })

  it('keeps focus on the mint-error Retry while the document is re-made', async () => {
    sandbox.failed = true
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(PAGE)
    mount()
    const band = await screen.findByTestId('crew-dashboard-mint-error-band')
    const retry = within(band).getByRole('button', { name: 'Try again' })

    retry.focus()
    await userEvent.keyboard('{Enter}')
    expect(sandbox.retries).toBe(1)
    expect(retry.getAttribute('aria-disabled')).toBe('true')
    expect(retry.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(retry)

    await userEvent.keyboard('{Enter}')
    expect(sandbox.retries).toBe(1)
  })
})
