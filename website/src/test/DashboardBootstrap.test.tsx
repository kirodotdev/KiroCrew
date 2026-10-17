import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import DashboardBootstrap from '../components/DashboardBootstrap'

const { refreshScheduler } = vi.hoisted(() => ({
  refreshScheduler: vi.fn(),
}))

vi.mock('../hooks/useRefreshScheduler', () => ({
  useRefreshScheduler: () => refreshScheduler(),
}))

vi.mock('../components/KiroPrerequisiteGate', () => ({
  default: () => <div>Setup gate</div>,
}))

describe('DashboardBootstrap', () => {
  it('mounts auth recovery even while the prerequisite gate blocks App', () => {
    render(<DashboardBootstrap><div>App</div></DashboardBootstrap>)

    expect(screen.getByText('Setup gate')).toBeInTheDocument()
    expect(screen.queryByText('App')).not.toBeInTheDocument()
    expect(refreshScheduler).toHaveBeenCalledOnce()
  })
})
