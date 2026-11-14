/**
 * The `/apps/<name>` launch URL of a built-in app that became a core surface
 * (Task Runner was the built-in app `projects`) opens that surface, while an app
 * a user installed under the same name stays its own app.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import AppPage from './AppPage'

const { getApp, navigate } = vi.hoisted(() => ({ getApp: vi.fn(), navigate: vi.fn() }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ name: 'projects' }),
  useNavigate: () => navigate,
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../components/InstalledAppSurface', () => ({
  default: () => <div data-testid="app-surface" />,
}))

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AppPage for a built-in that became a core surface', () => {
  it('opens Task Runner when nothing is installed under the old name', async () => {
    getApp.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    render(<AppPage />)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/projects', { replace: true }))
  })

  it('opens Task Runner from the built-in record the cleanup has not retired yet', async () => {
    getApp.mockResolvedValue({ name: 'projects', origin: 'builtin', enabled: true, manifest: { ui: { pages: [{ route: '/projects' }] } } })
    render(<AppPage />)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/projects', { replace: true }))
  })

  it('renders an app a user installed under the same name', async () => {
    getApp.mockResolvedValue({ name: 'projects', origin: 'local', enabled: true, manifest: { ui: { entry: 'index.js', pages: [{ route: '/' }] } } })
    render(<AppPage />)
    // One resolved getApp, then the mocked surface renders in the same commit.
    expect(await screen.findByTestId('app-surface', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(navigate).not.toHaveBeenCalled()
  })
})
