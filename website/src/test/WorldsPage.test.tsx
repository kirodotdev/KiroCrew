import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import WorldsPage from '../pages/WorldsPage'
import type { AgentSource } from '../hooks/useAgentSync'

// Mock useAgentSync to avoid real API calls
vi.mock('../hooks/useAgentSync', () => ({
  useAgentSync: () => ({
    agents: [
      { id: 'slot-1', name: 'Alpha', label: 'default', kind: 'slot', running: true, detail: '3 msgs' },
      { id: 'cron-1', name: 'Backup', label: 'cron', kind: 'cron', running: false, detail: '*/5 * * * *' },
    ],
    maxAgents: 8,
  }),
}))

const { mockOpenPopout, mockPopoutActive } = vi.hoisted(() => ({
  mockOpenPopout: vi.fn(),
  mockPopoutActive: { value: false },
}))

vi.mock('../hooks/usePopoutSync', () => ({
  usePopoutSync: () => ({ popoutActive: mockPopoutActive.value, broadcastScene: vi.fn(), openPopout: mockOpenPopout }),
}))

// Mock all scene components — they use canvas which jsdom doesn't support
vi.mock('../pages/scenes/OfficeScene', () => ({
  default: ({ agents }: { agents: AgentSource[] }) => <div data-testid="scene-office">OfficeScene ({agents?.length ?? 0})</div>,
}))
vi.mock('../pages/scenes/NeuralConstellationScene', () => ({
  default: ({ agents }: { agents: AgentSource[] }) => <div data-testid="scene-neural">NeuralScene ({agents.length})</div>,
}))
vi.mock('../pages/scenes/WizardTowerScene', () => ({
  default: ({ agents }: { agents: AgentSource[] }) => <div data-testid="scene-wizard">WizardScene ({agents.length})</div>,
}))
vi.mock('../pages/scenes/UnderwaterLabScene', () => ({
  default: ({ agents }: { agents: AgentSource[] }) => <div data-testid="scene-underwater">UnderwaterScene ({agents.length})</div>,
}))
vi.mock('../pages/scenes/mission-control/MissionControlScene', () => ({
  default: ({ agents }: { agents: AgentSource[] }) => <div data-testid="scene-mission">MissionScene ({agents.length})</div>,
}))

beforeEach(() => {
  localStorage.clear()
  mockPopoutActive.value = false
  mockOpenPopout.mockClear()
})

describe('WorldsPage', () => {
  describe('rendering', () => {
    it('renders page title', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByText(/Agent Worlds/)).toBeInTheDocument()
    })

    it('shows agent summary in subtitle', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByText(/2 agents present/)).toBeInTheDocument()
      expect(screen.getByText(/1 active/)).toBeInTheDocument()
      expect(screen.getByText(/6 slots open/)).toBeInTheDocument()
    })

    it('renders all scene picker buttons', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByText('Office')).toBeInTheDocument()
      expect(screen.getByText('Neural Net')).toBeInTheDocument()
      expect(screen.getByText('Wizard Tower')).toBeInTheDocument()
      expect(screen.getByText('Deep Lab')).toBeInTheDocument()
      expect(screen.getByText('Mission Control')).toBeInTheDocument()
    })
  })

  describe('scene switching', () => {
    it('shows office scene by default', () => {
      renderWithProviders(<WorldsPage />)
      const officeWrapper = screen.getByTestId('scene-office').parentElement!
      expect(officeWrapper.style.display).not.toBe('none')
    })

    it('switches to neural scene on click', () => {
      renderWithProviders(<WorldsPage />)
      fireEvent.click(screen.getByText('Neural Net'))
      const neuralWrapper = screen.getByTestId('scene-neural').parentElement!
      expect(neuralWrapper.style.display).not.toBe('none')
      const officeWrapper = screen.getByTestId('scene-office').parentElement!
      expect(officeWrapper.style.display).toBe('none')
    })

    it('switches to wizard scene on click', () => {
      renderWithProviders(<WorldsPage />)
      fireEvent.click(screen.getByText('Wizard Tower'))
      const wizardWrapper = screen.getByTestId('scene-wizard').parentElement!
      expect(wizardWrapper.style.display).not.toBe('none')
    })

    it('switches to underwater scene on click', () => {
      renderWithProviders(<WorldsPage />)
      fireEvent.click(screen.getByText('Deep Lab'))
      const underwaterWrapper = screen.getByTestId('scene-underwater').parentElement!
      expect(underwaterWrapper.style.display).not.toBe('none')
    })

    it('keeps all scenes mounted (hidden, not removed)', () => {
      renderWithProviders(<WorldsPage />)
      fireEvent.click(screen.getByText('Neural Net'))
      // All four scenes should still be in the DOM
      expect(screen.getByTestId('scene-office')).toBeInTheDocument()
      expect(screen.getByTestId('scene-neural')).toBeInTheDocument()
      expect(screen.getByTestId('scene-wizard')).toBeInTheDocument()
      expect(screen.getByTestId('scene-underwater')).toBeInTheDocument()
      expect(screen.getByTestId('scene-mission')).toBeInTheDocument()
    })
  })

  describe('localStorage persistence', () => {
    it('saves selected scene to localStorage', () => {
      renderWithProviders(<WorldsPage />)
      fireEvent.click(screen.getByText('Wizard Tower'))
      expect(localStorage.getItem('mc-agent-scene')).toBe('wizard')
    })

    it('restores scene from localStorage', () => {
      localStorage.setItem('mc-agent-scene', 'underwater')
      renderWithProviders(<WorldsPage />)
      const underwaterWrapper = screen.getByTestId('scene-underwater').parentElement!
      expect(underwaterWrapper.style.display).not.toBe('none')
      const officeWrapper = screen.getByTestId('scene-office').parentElement!
      expect(officeWrapper.style.display).toBe('none')
    })

    it('defaults to office when no saved scene', () => {
      renderWithProviders(<WorldsPage />)
      const officeWrapper = screen.getByTestId('scene-office').parentElement!
      expect(officeWrapper.style.display).not.toBe('none')
    })
  })

  describe('collapse toggle', () => {
    it('renders toggle button showing ▲ (expanded) by default', () => {
      renderWithProviders(<WorldsPage />)
      const toggle = screen.getByTitle('Hide controls')
      expect(toggle).toBeInTheDocument()
      expect(toggle).toHaveTextContent('▲')
    })

    it('collapses panel on click and expands on second click', () => {
      renderWithProviders(<WorldsPage />)
      const panel = screen.getByTestId('collapse-panel')
      const toggle = screen.getByTitle('Hide controls')

      fireEvent.click(toggle)
      expect(screen.getByTitle('Show controls')).toHaveTextContent('▼')
      expect(panel.style.gridTemplateRows).toBe('0fr')

      fireEvent.click(screen.getByTitle('Show controls'))
      expect(screen.getByTitle('Hide controls')).toHaveTextContent('▲')
      expect(panel.style.gridTemplateRows).toBe('1fr')
    })
  })

  describe('agents passed to scenes', () => {
    it('passes agents array to neural scene', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByTestId('scene-neural')).toHaveTextContent('NeuralScene (2)')
    })

    it('passes agents array to wizard scene', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByTestId('scene-wizard')).toHaveTextContent('WizardScene (2)')
    })

    it('passes agents array to underwater scene', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByTestId('scene-underwater')).toHaveTextContent('UnderwaterScene (2)')
    })
  })

  describe('popout', () => {
    it('renders popout button', () => {
      renderWithProviders(<WorldsPage />)
      expect(screen.getByTitle('Pop out to separate window')).toBeInTheDocument()
    })

    it('calls openPopout when popout button clicked', () => {
      renderWithProviders(<WorldsPage />)
      fireEvent.click(screen.getByTitle('Pop out to separate window'))
      expect(mockOpenPopout).toHaveBeenCalledOnce()
    })

    it('shows overlay when popout is active', () => {
      mockPopoutActive.value = true
      renderWithProviders(<WorldsPage />)
      expect(screen.getByText('Playing in popout window')).toBeInTheDocument()
      expect(screen.getByText('Focus popout')).toBeInTheDocument()
    })
  })
})
