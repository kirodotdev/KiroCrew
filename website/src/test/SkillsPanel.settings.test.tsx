import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
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
  // MemoryRouter: the panel links to the pending queue on Agent Capabilities →
  // Skills and forwards a `?review=` deep link there, so it needs a router.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
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

describe('SkillsPanel – extra skill paths', () => {
  const EXTRA_PATH = 'skills.extra_paths'

  beforeEach(() => {
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockClear()
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({
        skills: { auto_create_from_sessions: false, approval_required: true, extra_paths: ['/opt/a', '~/b'] },
      }),
    )
  })

  /** The field once the stored list has loaded into it. */
  async function loadedField(initial: string) {
    const field = await screen.findByLabelText('Extra skill paths')
    await waitFor(() => expect(field).toHaveValue(initial))
    return field
  }

  it('shows the stored paths one per line', async () => {
    wrap(<SkillsPanel />)
    expect(await loadedField('/opt/a\n~/b')).toBeInstanceOf(HTMLTextAreaElement)
  })

  it('shows an empty field when no extra paths are stored', async () => {
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ skills: { auto_create_from_sessions: false, approval_required: true } }),
    )
    wrap(<SkillsPanel />)
    const field = await screen.findByLabelText('Extra skill paths')
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    expect(field).toHaveValue('')
  })

  it('connects the label, the description and the settings row to the field', async () => {
    wrap(<SkillsPanel />)
    const field = await loadedField('/opt/a\n~/b')
    const row = field.closest(`[data-setting-key="${EXTRA_PATH}"]`)
    expect(row).not.toBeNull()
    expect(row).toHaveTextContent(/one path per line/i)
    expect(row).toHaveTextContent(/blank lines are ignored/i)
    expect(field).toHaveAttribute('placeholder', 'One folder per line')
  })

  it('PATCHes the exact normalized list on blur: trimmed, blank lines dropped, order and duplicates kept', async () => {
    wrap(<SkillsPanel />)
    const field = await loadedField('/opt/a\n~/b')
    fireEvent.change(field, { target: { value: '  /x  \r\n\r\n/y\n   \n/x\n' } })
    expect(patchConfigMock).not.toHaveBeenCalled() // typing alone writes nothing
    fireEvent.blur(field)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(EXTRA_PATH, ['/x', '/y', '/x']))
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
  })

  it('saves an empty list when the field is cleared', async () => {
    wrap(<SkillsPanel />)
    const field = await loadedField('/opt/a\n~/b')
    fireEvent.change(field, { target: { value: '' } })
    fireEvent.blur(field)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(EXTRA_PATH, []))
  })

  it('does not write when the edit normalizes back to the stored list', async () => {
    wrap(<SkillsPanel />)
    const field = await loadedField('/opt/a\n~/b')
    fireEvent.change(field, { target: { value: '\n  /opt/a  \n~/b\n\n' } })
    fireEvent.blur(field)
    await waitFor(() => expect(field).toHaveValue('/opt/a\n~/b'))
    expect(patchConfigMock).not.toHaveBeenCalledWith(EXTRA_PATH, expect.anything())
  })

  it('does not write on a blur with no edit', async () => {
    wrap(<SkillsPanel />)
    const field = await loadedField('/opt/a\n~/b')
    fireEvent.blur(field)
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})
