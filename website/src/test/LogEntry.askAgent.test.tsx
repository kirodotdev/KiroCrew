import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import LogEntry, { type LogEntryData } from '../components/LogEntry'
import { consumeChatHandoff } from '../utils/errorReport'
import { buildCronRunReport, MAX_TRACE_TAIL } from '../utils/cronRunReport.prompt'

// #7403: a failed or timed-out scheduled run hands its own trace to the agent
// from the history row, so debugging it is one click instead of copying the
// log into a new chat by hand. Success and user-cancelled rows offer nothing.

const cronRunDetail = vi.fn()
vi.mock('../api/client', () => ({
  api: { cronRunDetail: (...args: unknown[]) => cronRunDetail(...args) },
}))

function row(status: LogEntryData['status']): LogEntryData {
  return { run_id: 'run-42', status, started_at: 1_790_000_000, duration_ms: 1200, trigger: 'scheduled', summary: 'exit 1' }
}

async function openRow(status: LogEntryData['status']) {
  renderWithProviders(<LogEntry entry={row(status)} jobId="job-7" jobName="Nightly digest" />)
  fireEvent.click(screen.getByRole('button', { name: /exit 1/ }))
  await screen.findByText(/Traceback: boom/)
}

describe('LogEntry failed-run hand-off (#7403)', () => {
  beforeEach(() => {
    cronRunDetail.mockReset().mockResolvedValue({ trace: 'step 1 ok\nTraceback: boom' })
    sessionStorage.clear()
  })

  it.each(['failure', 'timeout'] as const)('offers Ask the agent on a %s row and stages the run context', async (status) => {
    await openRow(status)
    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff()
    expect(prompt).toContain('Scheduled job "Nightly digest"')
    expect(prompt).toContain('Job id: job-7')
    expect(prompt).toContain('Run id: run-42')
    expect(prompt).toContain('Traceback: boom')
    expect(cronRunDetail).toHaveBeenCalledWith('job-7', 'run-42')
  })

  it.each(['success', 'cancelled'] as const)('offers nothing on a %s row', async (status) => {
    await openRow(status)
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).not.toBeInTheDocument()
  })
})

describe('buildCronRunReport', () => {
  const base = { jobId: 'job-7', runId: 'run-42', status: 'failure' as const, startedAt: 1_790_000_000, trigger: 'manual' }

  it('keeps the TAIL of a long trace, where the failure is reported', () => {
    const trace = 'HEAD-MARKER\n' + 'x'.repeat(MAX_TRACE_TAIL * 2) + '\nTAIL-ERROR'
    const detail = buildCronRunReport({ ...base, trace }).detail!
    expect(detail).toContain('TAIL-ERROR')
    expect(detail).toContain('[earlier output truncated]')
    expect(detail).not.toContain('HEAD-MARKER')
  })

  it('scrubs credentials in the trace before cutting it', () => {
    const detail = buildCronRunReport({ ...base, trace: 'push to https://x-access-token:ghp_abcdefghijkl@github.com/o/r failed' }).detail!
    expect(detail).not.toContain('ghp_abcdefghijkl')
    expect(detail).toContain('[redacted]@github.com')
  })

  it('falls back to the job id when the job has no name, and names a timeout as one', () => {
    expect(buildCronRunReport({ ...base, status: 'timeout' }).message).toBe('Scheduled job "job-7" timed out.')
  })
})
