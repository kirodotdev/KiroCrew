import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

import { api } from '../api/client'
import CapabilitiesPage from '../pages/CapabilitiesPage'
import { renderWithProviders } from './helpers'

vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/AgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))
vi.mock('../components/RestartButton', () => ({ default: () => <div data-testid="restart-button" /> }))
vi.mock('../hooks/useConnectionsUi', () => ({ useConnectionsUiEnabled: () => false }))
vi.mock('../api/client', () => ({
  api: {
    projectBundles: vi.fn(),
    createProjectBundle: vi.fn(),
    addProjectBundle: vi.fn(),
    syncProjectBundle: vi.fn(),
    activateProjectBundle: vi.fn(),
    deactivateProjectBundle: vi.fn(),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  vi.mocked(api.projectBundles).mockResolvedValue({ projects: [] })
})

describe('Agent Capabilities — Projects tab', () => {
  it('renders the Projects bundle manager inside Agent Capabilities', async () => {
    renderWithProviders(<CapabilitiesPage />, { route: '/capabilities?tab=projects' })

    expect(screen.getByRole('button', { name: 'Projects' })).toBeInTheDocument()
    expect(await screen.findByText('No projects yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create project' })).toBeInTheDocument()
    expect(screen.getByTestId('restart-button')).toBeInTheDocument()
  })
})
