import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import HooksPage from '../src/pages/HooksPage'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

const renderWithRouter = (component: React.ReactElement) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{component}</BrowserRouter>
    </QueryClientProvider>
  )
}

// Mock window.confirm for delete operations
const mockConfirm = vi.fn()
Object.defineProperty(window, 'confirm', {
  writable: true,
  value: mockConfirm,
})

const mockHooks = [
  {
    id: 'hook-1',
    name: 'Deploy Notifier',
    event: 'UserPromptSubmit',
    matcher: '*deploy*',
    command: 'echo "Deploy started"',
    timeout: 30,
    enabled: true,
    last_run: Math.floor(Date.now() / 1000) - 7200,   // 2 hours ago
    last_status: 'ok',
    run_count: 15,
  },
  {
    id: 'hook-2',
    name: 'Git Guard',
    event: 'PreToolUse',
    matcher: '@git/*',
    command: './scripts/git-check.sh',
    timeout: 10,
    enabled: false,
    last_run: Math.floor(Date.now() / 1000) - 172800, // 2 days ago
    last_status: 'error',
    run_count: 8,
  },
]

describe('HooksPage Integration Tests', () => {
  /** Shared helper: render page, wait for data, click "+ New Hook", wait for form */
  async function openNewHookForm() {
    const user = userEvent.setup()
    renderWithRouter(<HooksPage />)
    await waitFor(() => { expect(screen.getByRole('button', { name: /\+ new hook/i })).toBeInTheDocument() })
    await user.click(screen.getByRole('button', { name: /\+ new hook/i }))
    await waitFor(() => { expect(screen.getByPlaceholderText(/hook name/i)).toBeInTheDocument() })
    return user
  }

  beforeEach(() => {
    vi.clearAllMocks()
    mockConfirm.mockReturnValue(true)

    // Set up default hooks endpoint
    server.use(
      http.get('/api/hooks', () => {
        return HttpResponse.json({ hooks: mockHooks })
      })
    )
  })

  it('loads and displays hooks on mount', async () => {
    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Deploy Notifier')).toBeInTheDocument()
      expect(screen.getByText('Git Guard')).toBeInTheDocument()
    }, { timeout: 5000 })
  })

  it('displays hook details correctly', async () => {
    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Deploy Notifier')).toBeInTheDocument()
    })

    // Check event badges
    expect(screen.getByText('UserPromptSubmit')).toBeInTheDocument()
    expect(screen.getByText('PreToolUse')).toBeInTheDocument()

    // Check matchers (rendered in table cells)
    expect(screen.getByText('*deploy*')).toBeInTheDocument()
    expect(screen.getByText('@git/*')).toBeInTheDocument()

    // Check commands
    expect(screen.getByText(/echo "Deploy started"/)).toBeInTheDocument()
    expect(screen.getByText(/\.\/scripts\/git-check\.sh/)).toBeInTheDocument()

    // Check run counts (rendered as numbers in table)
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText('8')).toBeInTheDocument()

    // Check statuses (rendered as Badge components with uppercase text)
    expect(screen.getByText('OK')).toBeInTheDocument()
    expect(screen.getByText('Error')).toBeInTheDocument()
  })

  it('shows empty state when no hooks exist', async () => {
    server.use(
      http.get('/api/hooks', () => {
        return HttpResponse.json({ hooks: [] })
      })
    )

    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('No hooks yet')).toBeInTheDocument()
      expect(screen.getByText(/Create a hook to run scripts on chat events/)).toBeInTheDocument()
    })
  })

  it('creates a new hook', async () => {
    const user = userEvent.setup()

    let capturedBody: any
    server.use(
      http.post('/api/hooks', async ({ request }) => {
        capturedBody = await request.json()
        return HttpResponse.json(
          {
            id: 'hook-3',
            ...capturedBody,
            last_run: 0,
            last_status: '',
            run_count: 0,
          },
          { status: 201 }
        )
      })
    )

    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /\+ new hook/i })).toBeInTheDocument()
    })

    // Click "+ New Hook" button
    const newButton = screen.getByRole('button', { name: /\+ new hook/i })
    await user.click(newButton)

    // Should show create form
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/hook name/i)).toBeInTheDocument()
    })

    // Fill in hook details
    const nameInput = screen.getByPlaceholderText(/hook name/i)
    await user.type(nameInput, 'Test Hook')

    const commandInput = screen.getByPlaceholderText(/echo 'hook fired'/i)
    await user.type(commandInput, 'echo "Test command"')

    const matcherInput = screen.getByPlaceholderText(/matcher.*optional/i)
    await user.type(matcherInput, '*test*')

    // Click save button
    const saveButton = screen.getByRole('button', { name: /^save$/i })
    await user.click(saveButton)

    // Verify the request was made with correct data
    await waitFor(() => {
      expect(capturedBody).toBeDefined()
      expect(capturedBody.name).toBe('Test Hook')
      expect(capturedBody.command).toBe('echo "Test command"')
      expect(capturedBody.matcher).toBe('*test*')
      expect(capturedBody.event).toBe('UserPromptSubmit')
      expect(capturedBody.timeout).toBe(30)
    })

    // Form should be hidden after creation
    await waitFor(() => {
      expect(screen.queryByPlaceholderText(/hook name/i)).not.toBeInTheDocument()
    })
  })

  it('cancels hook creation', async () => {
    const user = await openNewHookForm()

    // Click cancel
    const cancelButton = screen.getByRole('button', { name: /cancel/i })
    await user.click(cancelButton)

    // Form should be hidden
    await waitFor(() => {
      expect(screen.queryByPlaceholderText(/hook name/i)).not.toBeInTheDocument()
    })
  })

  it('edits an existing hook', async () => {
    const user = userEvent.setup()

    let capturedBody: any
    server.use(
      http.put('/api/hooks/:id', async ({ request }) => {
        capturedBody = await request.json()
        return HttpResponse.json({ success: true })
      })
    )

    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Deploy Notifier')).toBeInTheDocument()
    })

    // Find the hook row and click edit
    const editButtons = screen.getAllByRole('button', { name: /edit/i })
    // First hook is "Deploy Notifier", second is "Git Guard"
    await user.click(editButtons[0])

    // Should show edit form
    await waitFor(() => {
      const nameInputs = screen.getAllByDisplayValue('Deploy Notifier')
      expect(nameInputs.length).toBeGreaterThan(0)
    })

    // Update the name
    const nameInput = screen.getByDisplayValue('Deploy Notifier')
    fireEvent.change(nameInput, { target: { value: 'Updated Deploy Hook' } })

    // Click save
    const saveButton = screen.getByRole('button', { name: /^save$/i })
    await user.click(saveButton)

    // Verify the update request was made
    await waitFor(() => {
      expect(capturedBody).toBeDefined()
      expect(capturedBody.name).toBe('Updated Deploy Hook')
    })

    // Form should be hidden after update
    await waitFor(() => {
      expect(screen.queryByDisplayValue('Updated Deploy Hook')).not.toBeInTheDocument()
    })
  })

  it('toggles hook enabled state', async () => {
    const user = userEvent.setup()

    let toggledHookId: string | undefined
    server.use(
      http.post('/api/hooks/:id/toggle', async ({ params }) => {
        toggledHookId = params.id as string
        return HttpResponse.json({ success: true })
      })
    )

    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Deploy Notifier')).toBeInTheDocument()
    })

    // Find the toggle switch for the first hook (Deploy Notifier is enabled)
    const toggleButton = screen.getByRole('button', { name: /disable hook/i })
    await user.click(toggleButton)

    // Verify toggle was called
    await waitFor(() => {
      expect(toggledHookId).toBe('hook-1')
    })
  })

  it('deletes a hook', async () => {
    const user = userEvent.setup()

    let deletedHookId: string | undefined
    server.use(
      http.delete('/api/hooks/:id', async ({ params }) => {
        deletedHookId = params.id as string
        return HttpResponse.json({ success: true })
      })
    )

    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Git Guard')).toBeInTheDocument()
    })

    // Find the Delete button for Git Guard (second hook)
    const deleteButtons = screen.getAllByRole('button', { name: /delete/i })
    await user.click(deleteButtons[1])

    // Should show confirmation dialog
    expect(mockConfirm).toHaveBeenCalledWith(expect.stringContaining('Git Guard'))

    // Verify delete was called
    await waitFor(() => {
      expect(deletedHookId).toBe('hook-2')
    })
  })

  it('tests a hook', async () => {
    const user = userEvent.setup()

    server.use(
      http.post('/api/hooks/:id/test', async () => {
        return HttpResponse.json({
          result: {
            exit_code: 0,
            duration_ms: 123,
            stdout: 'Deploy started\n',
            stderr: '',
          },
        })
      })
    )

    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Deploy Notifier')).toBeInTheDocument()
    })

    // Find the Test button for Deploy Notifier (first hook)
    const testButtons = screen.getAllByRole('button', { name: /^test$/i })
    await user.click(testButtons[0])

    // Should show test results
    await waitFor(() => {
      expect(screen.getByText(/test result/i)).toBeInTheDocument()
      expect(screen.getByText(/123ms/)).toBeInTheDocument()
      expect(screen.getByText('Deploy started')).toBeInTheDocument()
    })
  })

  it('displays error message when API fails', async () => {
    server.use(
      http.get('/api/hooks', () => {
        return HttpResponse.json(
          { error: 'Internal server error' },
          { status: 500 }
        )
      })
    )

    renderWithRouter(<HooksPage />)

    // Component should display an error message with text matching the error
    await waitFor(() => {
      expect(screen.getByText('Error')).toBeInTheDocument()
      expect(screen.getByText(/failed to load hooks/i)).toBeInTheDocument()
    }, { timeout: 2000 })
  })

  it('changes event type and updates matcher placeholder', async () => {
    await openNewHookForm()

    // The event picker is a SimpleSelect (Radix Select) now, so there is no
    // native select to `selectOptions` — open the trigger, then click the row.
    // fireEvent rather than userEvent: Radix commits discrete events through
    // flushSync, which userEvent's act() wrapper turns into "Should not already
    // be working."
    fireEvent.click(screen.getByRole('combobox', { name: 'Event' }))
    fireEvent.click(await screen.findByRole('option', { name: 'PreToolUse' }))

    // Matcher placeholder should change to tool filter placeholder
    await waitFor(() => {
      const matcherInput = screen.getByPlaceholderText(/tool filter.*fs_write/i)
      expect(matcherInput).toBeInTheDocument()
    })
  })

  it('updates timeout value', async () => {
    const user = await openNewHookForm()

    // Find timeout input - may be a number input with value 30
    const inputs = screen.getAllByRole('spinbutton') as HTMLInputElement[]
    const timeoutInput = inputs.find(input => input.value === '30')
    expect(timeoutInput).toBeDefined()

    fireEvent.change(timeoutInput!, { target: { value: '60' } })
    expect(timeoutInput).toHaveValue(60)
  })

  it('displays time ago correctly', async () => {
    renderWithRouter(<HooksPage />)

    await waitFor(() => {
      expect(screen.getByText('Deploy Notifier')).toBeInTheDocument()
    })

    // Should show time ago format
    expect(screen.getByText(/2h ago/i)).toBeInTheDocument()
    expect(screen.getByText(/2d ago/i)).toBeInTheDocument()
  })
})
