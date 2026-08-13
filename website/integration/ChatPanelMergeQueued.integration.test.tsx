import { describe, it, expect, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { ChatPanel } from '../src/pages/settings/ChatPanel'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

const DEFAULT_DASH_CFG = { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false }

describe('ChatPanel – Merge Queued Messages', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/dashboard/config', () => HttpResponse.json(DEFAULT_DASH_CFG)),
      http.put('/api/dashboard/config', async ({ request }) => {
        await request.json()
        return HttpResponse.json({ ok: true })
      }),
      http.get('/api/chat/config', () => HttpResponse.json({
        historyExpanded: false, notifLimit: 50, collapseAllSteps: true,
      })),
    )
  })

  it('renders the Merge Queued Messages toggle', async () => {
    renderWithProviders(<ChatPanel />)
    await waitFor(() => {
      expect(screen.getByText('Merge Queued Messages')).toBeInTheDocument()
    })
  })

  it('toggle is off by default', async () => {
    renderWithProviders(<ChatPanel />)
    await waitFor(() => {
      expect(screen.getByText('Merge Queued Messages')).toBeInTheDocument()
    })
    const toggle = screen.getByRole('switch', { name: 'Merge Queued Messages' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })

  it('sends PUT when toggled on', async () => {
    let putBody: any = null
    server.use(
      http.put('/api/dashboard/config', async ({ request }) => {
        putBody = await request.json()
        return HttpResponse.json({ ok: true })
      }),
    )

    const user = userEvent.setup()
    renderWithProviders(<ChatPanel />)
    await waitFor(() => {
      expect(screen.getByText('Merge Queued Messages')).toBeInTheDocument()
    })

    const toggle = screen.getByRole('switch', { name: 'Merge Queued Messages' })
    await user.click(toggle)

    await waitFor(() => {
      expect(putBody).toBeTruthy()
      expect(putBody.merge_queued_messages).toBe(true)
    })
  })
})
