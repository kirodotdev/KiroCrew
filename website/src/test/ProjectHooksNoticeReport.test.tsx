import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const setProjectHooksConsent = vi.fn()
vi.mock('../api/client', () => ({ api: { setProjectHooksConsent: (...a: unknown[]) => setProjectHooksConsent(...a) } }))
// Stand-in that shows which report the notice hands to "Ask the agent".
vi.mock('../components/ErrorNotice', () => ({
  default: (props: { message?: string | null; report?: { id: string } }) =>
    props.message ? <p data-testid="handoff-report">{props.report?.id ?? 'none'}</p> : null,
}))

import ProjectHooksNotice from '../pages/chat/ProjectHooksNotice'
import type { ChatMessage } from '../types'
import { attachReport, type ErrorReport } from '../utils/errorReport'

const row = {
  role: 'notice',
  content: 'ℹ Project hooks are off for this folder.',
  meta: { kind: 'spec_hooks', project_hooks: 'off', project_hooks_folder: '/work/repo', project_hooks_commands: [] },
} as unknown as ChatMessage

describe('ProjectHooksNotice error hand-off', () => {
  it('passes the journaled report with the translated sentence', async () => {
    const report = { id: 'rep-1', at: 0, message: 'project_hooks_consent_write_failed' } as unknown as ErrorReport
    setProjectHooksConsent.mockImplementation(async () => {
      throw attachReport(new Error('project_hooks_consent_write_failed'), report)
    })
    render(<ProjectHooksNotice message={row} askAgent />)
    fireEvent.click(screen.getByTestId('project-hooks-notice-toggle'))
    await waitFor(() => expect(screen.getByTestId('handoff-report').textContent).toBe('rep-1'))
  })
})
