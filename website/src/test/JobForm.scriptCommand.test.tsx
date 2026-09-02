import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm, { parseJobDefaults, buildBody, jobKindOf } from '../components/JobForm'
import type { CronJob } from '../types'

vi.mock('../api/client', () => ({ api: { updateCron: vi.fn(), createCron: vi.fn() } }))

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'sc1', name: 'nba-progress-nudge', message: '', schedule: '', enabled: true,
    cron_expr: '47 21 * * 1-5', ...overrides,
  } as CronJob
}

describe('JobForm script/command edit path', () => {
  it('jobKindOf derives the execution kind from the carried field', () => {
    expect(jobKindOf(makeJob({ script: '~/.kirocrew/crons/f.py:run' }))).toBe('script')
    expect(jobKindOf(makeJob({ command: 'echo hi' }))).toBe('command')
    expect(jobKindOf(makeJob({ message: 'do the thing' }))).toBe('message')
    expect(jobKindOf(undefined)).toBe('message')
  })

  it('parseJobDefaults seeds jobKind=script for a script cron with empty message', () => {
    const result = parseJobDefaults(makeJob({ script: '~/.kirocrew/crons/nba_progress_nudge.py:run' }))
    expect(result.jobKind).toBe('script')
    expect(result.message).toBe('')
    expect(result.schedMode).toBe('weekly')
  })

  it('buildBody does NOT require a message for a script cron and omits message/agent', () => {
    let error = ''
    const f = { ...parseJobDefaults(makeJob({ script: '~/.kirocrew/crons/f.py:run' })) }
    const body = buildBody(f, 'America/Chicago', e => { error = e })
    expect(error).toBe('')
    expect(body).not.toBeNull()
    // No "Message is required" error — message/agent are omitted so the
    // partial PATCH preserves the script binding (backend ignores script/command).
    expect(body!.message).toBeUndefined()
    expect(body!.agent).toBeUndefined()
    expect(body!.approval_mode).toBeUndefined()
    expect(body!.script).toBeUndefined()
    // Schedule fields ARE sent.
    expect(body!.cron).toBe('47 21 * * 1,2,3,4,5')
    expect(body!.timezone).toBe('America/Chicago')
    expect(body!.name).toBe('nba-progress-nudge')
  })

  it('buildBody does NOT require a message for a command cron and omits message/agent', () => {
    let error = ''
    const f = { ...parseJobDefaults(makeJob({ command: 'python3 cleanup.py', cron_expr: '0 3 * * *' })) }
    const body = buildBody(f, 'UTC', e => { error = e })
    expect(error).toBe('')
    expect(body).not.toBeNull()
    expect(body!.message).toBeUndefined()
    expect(body!.agent).toBeUndefined()
    expect(body!.command).toBeUndefined()
    expect(body!.name).toBe('nba-progress-nudge')
  })

  it('buildBody changing only the schedule of a script cron still saves (interval mode)', () => {
    let error = ''
    const base = parseJobDefaults(makeJob({ script: '~/.kirocrew/crons/f.py:run' }))
    const f = { ...base, schedMode: 'interval' as const, intVal: 30, intUnit: 'minutes' as const }
    const body = buildBody(f, 'UTC', e => { error = e })
    expect(error).toBe('')
    expect(body).not.toBeNull()
    expect(body!.every).toBe(1800)
    expect(body!.message).toBeUndefined()
  })

  it('buildBody still requires a message for the agent/message kind (name present, only message missing)', () => {
    let error = ''
    const f = { ...parseJobDefaults(makeJob({ message: '' })) } // message kind, blank message, name present
    const body = buildBody(f, 'UTC', e => { error = e })
    expect(body).toBeNull()
    // The earlier !name guard already returned when name is missing, so by this
    // branch only the message can be absent — report just the message.
    expect(error).toBe('Message is required')
  })

  it('buildBody requires a name for every kind', () => {
    let error = ''
    const f = { ...parseJobDefaults(makeJob({ script: '~/.kirocrew/crons/f.py:run', name: '' })) }
    const body = buildBody(f, 'UTC', e => { error = e })
    expect(body).toBeNull()
    expect(error).toBe('Name is required')
  })

  it('renders the script value read-only and hides the Agent selector in the detail panel', () => {
    renderWithProviders(
      <JobForm
        job={makeJob({ script: '~/.kirocrew/crons/nba_progress_nudge.py:run' })}
        agents={[{ name: 'kirocrew', description: '' } as never]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    // Script value shown
    expect(screen.getByText('~/.kirocrew/crons/nba_progress_nudge.py:run')).toBeInTheDocument()
    // "Script" label present, no "Message" textarea label, no "Agent" label
    expect(screen.getByText('Script')).toBeInTheDocument()
    expect(screen.queryByText('Agent')).not.toBeInTheDocument()
    expect(screen.queryByText('Approval')).not.toBeInTheDocument()
    // Channel still available for all kinds
    expect(screen.getByText('Channel ID')).toBeInTheDocument()
  })

  it('renders the agent selector for a normal message cron (unchanged behavior)', () => {
    renderWithProviders(
      <JobForm
        job={makeJob({ message: 'do the thing' })}
        agents={[{ name: 'kirocrew', description: '' } as never]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    expect(screen.getByText('Agent')).toBeInTheDocument()
    expect(screen.getByText('Approval')).toBeInTheDocument()
  })
})
