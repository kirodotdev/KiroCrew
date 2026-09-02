import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ skills: { auto_create_from_sessions: false, approval_required: true } }),
  ),
}))

vi.mock('../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock },
}))

import { SkillsPanel } from '../pages/settings/SkillsPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('SkillsPanel – auto-generate toggle', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockClear()
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ skills: { auto_create_from_sessions: false, approval_required: true } }),
    )
  })

  it('renders reflecting server state (auto-generate off by default)', async () => {
    wrap(<SkillsPanel />)
    expect(await screen.findByText('Auto-generate skills from sessions')).toBeInTheDocument()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
  })

  it('PATCHes skills.auto_create_from_sessions=true when toggled on', async () => {
    wrap(<SkillsPanel />)
    const label = await screen.findByText('Auto-generate skills from sessions')
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    fireEvent.click(label)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('skills.auto_create_from_sessions', true),
    )
  })

  it('disables the approval toggle while auto-generate is off', async () => {
    wrap(<SkillsPanel />)
    // With auto-create off, flipping approval must not fire a PATCH.
    const approval = await screen.findByText('Require approval before generated skills go live')
    fireEvent.click(approval)
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    expect(patchConfigMock).not.toHaveBeenCalledWith('skills.approval_required', expect.anything())
  })
})
