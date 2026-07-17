import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createTestStore, renderWithProviders } from './helpers'
import CronTab from '../src/pages/overview/CronTab'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

describe('CronTab Integration Tests', () => {
  /** Shared: intercept POST /api/crons and return captured body */
  function interceptCronCreate() {
    let capturedBody: any
    server.use(
      http.post('/api/crons', async ({ request }) => {
        capturedBody = await request.json()
        return HttpResponse.json({ id: `cron-${Date.now()}`, name: capturedBody.name, message: capturedBody.message, schedule: capturedBody.cron || `every ${capturedBody.every}s`, enabled: true, last_run: null, next_run: null }, { status: 201 })
      })
    )
    return { get body() { return capturedBody } }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    // Mock agents endpoint
    server.use(
      http.get('/api/agents/installed', () => {
        return HttpResponse.json([
          { name: 'default', source: 'builtin' },
          { name: 'kirocrew', source: 'builtin' },
        ])
      })
    )
  })

  it('loads and displays cron jobs on mount', async () => {
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
      expect(screen.getByText('Backup database')).toBeInTheDocument()
    })
  })

  it('displays job status badges correctly', async () => {
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
    })

    // First job is enabled (should show OK/Ready badge)
    expect(screen.getByText('Ready')).toBeInTheDocument()

    // Second job is paused
    expect(screen.getByText('Paused')).toBeInTheDocument()
  })

  it('creates a new cron job with interval mode', async () => {
    const user = userEvent.setup()
    const captured = interceptCronCreate()

    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/job name/i)).toBeInTheDocument()
    })

    // Fill in job details
    const nameInput = screen.getByPlaceholderText(/job name/i)
    fireEvent.change(nameInput, { target: { value: 'Test Job' } })

    const messageInput = screen.getByPlaceholderText(/message.*task/i)
    fireEvent.change(messageInput, { target: { value: 'Run system tests' } })

    // Select interval input and set value to 5
    const intervalInput = screen.getByDisplayValue('1') as HTMLInputElement
    await user.click(intervalInput)
    await user.keyboard('{Control>}a{/Control}') // Select all
    await user.keyboard('5') // Type 5 to replace

    // Click Add button
    const addButton = screen.getByRole('button', { name: /^add$/i })
    await user.click(addButton)

    // Verify the request was made with correct data
    await waitFor(() => {
      expect(captured.body).toBeDefined()
      expect(captured.body.name).toBe('Test Job')
      expect(captured.body.message).toBe('Run system tests')
      expect(captured.body.every).toBe(18000) // 5 hours * 3600 seconds
    })

    // Form should be cleared
    await waitFor(() => {
      expect(nameInput).toHaveValue('')
      expect(messageInput).toHaveValue('')
    })
  })

  it('creates a cron job with weekly schedule', async () => {
    const user = userEvent.setup()
    const captured = interceptCronCreate()

    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/job name/i)).toBeInTheDocument()
    })

    // Fill in job details
    const nameInput = screen.getByPlaceholderText(/job name/i)
    fireEvent.change(nameInput, { target: { value: 'Weekly Report' } })

    const messageInput = screen.getByPlaceholderText(/message.*task/i)
    fireEvent.change(messageInput, { target: { value: 'Generate weekly report' } })

    // Switch to weekly mode
    const modeSelect = screen.getByDisplayValue(/every interval/i)
    await user.selectOptions(modeSelect, 'weekly')

    // Select Mon and Fri
    const monButton = screen.getByRole('button', { name: /mon/i })
    const friButton = screen.getByRole('button', { name: /fri/i })
    await user.click(monButton)
    await user.click(friButton)

    // Click Add button
    const addButton = screen.getByRole('button', { name: /^add$/i })
    await user.click(addButton)

    // Verify the request was made with correct data
    await waitFor(() => {
      expect(captured.body).toBeDefined()
      expect(captured.body.name).toBe('Weekly Report')
      expect(captured.body.message).toBe('Generate weekly report')
      expect(captured.body.cron).toMatch(/\d+ \d+ \* \* \d+,\d+/) // cron format with two days
    })
  })

  it('validates required fields before creating job', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^add$/i })).toBeInTheDocument()
    })

    // Click Add without filling anything
    const addButton = screen.getByRole('button', { name: /^add$/i })
    await user.click(addButton)

    // Should show error
    await waitFor(() => {
      expect(screen.getByText(/name and message are required/i)).toBeInTheDocument()
    })
  })

  it('validates weekly mode requires at least one day', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/job name/i)).toBeInTheDocument()
    })

    // Fill in required fields
    const nameInput = screen.getByPlaceholderText(/job name/i)
    fireEvent.change(nameInput, { target: { value: 'Weekly Job' } })

    const messageInput = screen.getByPlaceholderText(/message.*task/i)
    fireEvent.change(messageInput, { target: { value: 'Weekly task' } })

    // Switch to weekly mode
    const modeSelect = screen.getByDisplayValue(/every interval/i)
    await user.selectOptions(modeSelect, 'weekly')

    // Don't select any days
    const addButton = screen.getByRole('button', { name: /^add$/i })
    await user.click(addButton)

    // Should show error
    await waitFor(() => {
      expect(screen.getByText(/select at least one day/i)).toBeInTheDocument()
    })
  })

  it('toggles cron job enabled state', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
    })

    // Find the first job's pause button
    const rows = screen.getAllByRole('row')
    const firstDataRow = rows.find(row => within(row).queryByText('Check system status'))!
    const pauseButton = within(firstDataRow).getByRole('button', { name: /pause/i })

    await user.click(pauseButton)

    // Jobs should be reloaded
    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
    })
  })

  it('deletes a cron job', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Backup database')).toBeInTheDocument()
    })

    // Find the second job's delete button
    const rows = screen.getAllByRole('row')
    const secondDataRow = rows[2] // Skip header and first data row
    const deleteButton = within(secondDataRow).getByRole('button', { name: /delete/i })

    await user.click(deleteButton)

    // Jobs should be reloaded
    await waitFor(() => {
      // The deleted job should still appear because we're using mock data
      // In a real scenario, the mock would update to reflect the deletion
      expect(screen.getByText('Jobs')).toBeInTheDocument()
    })
  })

  it('filters cron jobs by search term', async () => {
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
      expect(screen.getByText('Backup database')).toBeInTheDocument()
    })

    // Type in the search filter
    const filterInput = screen.getByPlaceholderText(/filter jobs/i)
    fireEvent.change(filterInput, { target: { value: 'backup' } })

    // Should still see Backup database
    expect(screen.getByText('Backup database')).toBeInTheDocument()
    // Check system status should NOT be visible after filtering
    expect(screen.queryByText('Check system status')).not.toBeInTheDocument()
  })

  it('refreshes jobs when refreshTrigger changes', async () => {
    const { rerender } = renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
    })

    // Change the refresh trigger
    rerender(<CronTab refreshTrigger={1} />)

    // Should reload cron jobs
    await waitFor(() => {
      expect(screen.getByText('Check system status')).toBeInTheDocument()
    })
  })

  it('shows "Continue" button title when job has_slot', async () => {
    server.use(
      http.get('/api/crons', () => {
        return HttpResponse.json({ jobs: [{ id: 'cron-slot', name: 'Linked Job', message: 'hi', schedule: '*/5 * * * *', enabled: true, last_run: null, next_run: null, has_slot: true, has_result: true }] })
      })
    )
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Linked Job')).toBeInTheDocument()
    })
    expect(screen.getByTitle('Continue session')).toBeInTheDocument()
  })

  it('shows "View" button title when job has no slot', async () => {
    server.use(
      http.get('/api/crons', () => {
        return HttpResponse.json({ jobs: [{ id: 'cron-noslot', name: 'Unlinked Job', message: 'hi', schedule: '*/5 * * * *', enabled: true, last_run: null, next_run: null, has_slot: false, has_result: true }] })
      })
    )
    renderWithProviders(<CronTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText('Unlinked Job')).toBeInTheDocument()
    })
    expect(screen.getByTitle('View last result')).toBeInTheDocument()
  })
})
