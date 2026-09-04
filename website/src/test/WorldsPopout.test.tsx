import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import WorldsPopout from '../pages/WorldsPopout'

vi.mock('../hooks/useAgentSync', () => ({
  useAgentSync: () => ({
    agents: [
      { id: 'slot-1', name: 'Alpha', label: 'default', kind: 'slot', running: true, detail: '3 msgs' },
    ],
    maxAgents: 8,
  }),
}))

const { mockBroadcastScene } = vi.hoisted(() => ({
  mockBroadcastScene: vi.fn(),
}))

vi.mock('../hooks/usePopoutSync', () => ({
  usePopoutSync: () => ({ popoutActive: false, broadcastScene: mockBroadcastScene, openPopout: vi.fn() }),
}))

vi.mock('../pages/scenes/OfficeScene', () => ({ default: (p: { agents?: unknown[] }) => <div data-testid="scene-office">OfficeScene ({p.agents?.length ?? 0})</div> }))
vi.mock('../pages/scenes/NeuralConstellationScene', () => ({ default: (p: { agents: unknown[] }) => <div data-testid="scene-neural">NeuralScene ({p.agents.length})</div> }))
vi.mock('../pages/scenes/WizardTowerScene', () => ({ default: (p: { agents: unknown[] }) => <div data-testid="scene-wizard">WizardScene ({p.agents.length})</div> }))
vi.mock('../pages/scenes/UnderwaterLabScene', () => ({ default: (p: { agents: unknown[] }) => <div data-testid="scene-underwater">UnderwaterScene ({p.agents.length})</div> }))
vi.mock('../pages/scenes/mission-control/MissionControlScene', () => ({ default: (p: { agents: unknown[] }) => <div data-testid="scene-mission">MissionScene ({p.agents.length})</div> }))

beforeEach(() => { localStorage.clear(); mockBroadcastScene.mockClear() })

describe('WorldsPopout', () => {
  it('renders all scene tab buttons', () => {
    renderWithProviders(<WorldsPopout />)
    expect(screen.getByText('Office')).toBeInTheDocument()
    expect(screen.getByText('Neural Net')).toBeInTheDocument()
    expect(screen.getByText('Wizard Tower')).toBeInTheDocument()
    expect(screen.getByText('Deep Lab')).toBeInTheDocument()
    expect(screen.getByText('Mission Control')).toBeInTheDocument()
  })

  it('shows office scene by default', () => {
    renderWithProviders(<WorldsPopout />)
    const officeWrapper = screen.getByTestId('scene-office').parentElement!
    expect(officeWrapper.style.display).not.toBe('none')
  })

  it('switches scene on tab click', () => {
    renderWithProviders(<WorldsPopout />)
    fireEvent.click(screen.getByText('Wizard Tower'))
    const wizardWrapper = screen.getByTestId('scene-wizard').parentElement!
    expect(wizardWrapper.style.display).not.toBe('none')
    const officeWrapper = screen.getByTestId('scene-office').parentElement!
    expect(officeWrapper.style.display).toBe('none')
  })

  it('keeps all scenes mounted when switching', () => {
    renderWithProviders(<WorldsPopout />)
    fireEvent.click(screen.getByText('Neural Net'))
    expect(screen.getByTestId('scene-office')).toBeInTheDocument()
    expect(screen.getByTestId('scene-neural')).toBeInTheDocument()
    expect(screen.getByTestId('scene-wizard')).toBeInTheDocument()
    expect(screen.getByTestId('scene-underwater')).toBeInTheDocument()
    expect(screen.getByTestId('scene-mission')).toBeInTheDocument()
  })

  it('persists scene selection to localStorage', () => {
    renderWithProviders(<WorldsPopout />)
    fireEvent.click(screen.getByText('Deep Lab'))
    expect(localStorage.getItem('mc-agent-scene')).toBe('underwater')
  })

  it('restores scene from localStorage', () => {
    localStorage.setItem('mc-agent-scene', 'wizard')
    renderWithProviders(<WorldsPopout />)
    const wizardWrapper = screen.getByTestId('scene-wizard').parentElement!
    expect(wizardWrapper.style.display).not.toBe('none')
  })

  it('has collapse/expand toggle', () => {
    renderWithProviders(<WorldsPopout />)
    const toggle = screen.getByTitle('Hide controls')
    expect(toggle).toHaveTextContent('▲')
    fireEvent.click(toggle)
    expect(screen.getByTitle('Show controls')).toHaveTextContent('▼')
  })

  it('passes agents to scenes', () => {
    renderWithProviders(<WorldsPopout />)
    expect(screen.getByTestId('scene-office')).toHaveTextContent('OfficeScene (1)')
  })

  it('broadcasts scene change', () => {
    renderWithProviders(<WorldsPopout />)
    fireEvent.click(screen.getByText('Wizard Tower'))
    expect(mockBroadcastScene).toHaveBeenCalledWith('wizard')
  })
})
