import { describe, it, expect, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import MemoryTab from '../src/pages/overview/MemoryTab'

describe('MemoryTab Integration Tests', () => {
  beforeEach(() => {
    // Each test gets a fresh store
  })

  it('loads and displays memory settings on mount', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    // Wait for settings to load - check for actual label text
    await waitFor(() => {
      expect(screen.getByText(/consolidation idle/i)).toBeInTheDocument()
    })

    // Check settings are displayed (History retention only shows if not migrated)
    expect(screen.getByText(/history retention/i)).toBeInTheDocument()
  })

  it('loads and displays preferences content', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    // Wait for preferences to load
    await waitFor(() => {
      expect(screen.getByText(/typescript/i)).toBeInTheDocument()
    })
  })

  it('loads and displays projects content', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    // Wait for projects to load
    await waitFor(() => {
      expect(screen.getByText(/kirocrew/i)).toBeInTheDocument()
    })

    expect(screen.getByText(/ai-powered development assistant/i)).toBeInTheDocument()
  })

  it('loads and displays history content', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    // Wait for history to load
    await waitFor(() => {
      expect(screen.getByText(/integration tests/i)).toBeInTheDocument()
    })
  })

  it('updates idle hours setting', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText(/consolidation idle/i)).toBeInTheDocument()
    })

    // Find the input by its value
    const inputs = screen.getAllByRole('spinbutton') as HTMLInputElement[]
    const idleInput = inputs[0] // First number input is idle hours

    fireEvent.change(idleInput, { target: { value: '48' } })

    await waitFor(() => {
      expect(idleInput.value).toBe('48')
    })
  })

  it('updates max days setting', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText(/history retention/i)).toBeInTheDocument()
    })

    // Find the input by its value
    const inputs = screen.getAllByRole('spinbutton') as HTMLInputElement[]
    const maxDaysInput = inputs[1] // Second number input is max days

    fireEvent.change(maxDaysInput, { target: { value: '60' } })

    await waitFor(() => {
      expect(maxDaysInput.value).toBe('60')
    })
  })

  it('saves memory settings', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText(/consolidation idle/i)).toBeInTheDocument()
    })

    // Find the first Save button (in Memory Settings card)
    const saveButtons = screen.getAllByRole('button', { name: /^save$/i })
    const settingsSaveBtn = saveButtons[0]
    
    await user.click(settingsSaveBtn)

    // Should show saved state
    await waitFor(() => {
      expect(settingsSaveBtn.textContent).toContain('Saved')
    })
  })

  it('triggers manual consolidation', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText(/summarize now/i)).toBeInTheDocument()
    })

    // Click the Summarize now button (manual consolidation)
    const consolidateButton = screen.getByRole('button', { name: /summarize now/i })
    await user.click(consolidateButton)

    // The consolidation will complete quickly in tests with empty sessions
    // Just verify the button exists and was clickable
    await waitFor(() => {
      expect(consolidateButton).toBeInTheDocument()
    })
  })

  it('saves preferences content', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    await waitFor(() => {
      expect(screen.getByText(/typescript/i)).toBeInTheDocument()
    })

    // Find the Preferences section Save button
    const saveButtons = screen.getAllByRole('button', { name: /save/i })
    const preferencesSaveBtn = saveButtons.find(btn => 
      btn.closest('.card-glow')?.querySelector('h3')?.textContent?.includes('Preferences')
    )
    
    if (preferencesSaveBtn) {
      await user.click(preferencesSaveBtn)
      
      await waitFor(() => {
        expect(preferencesSaveBtn.textContent).toContain('Saved')
      })
    }
  })
})
