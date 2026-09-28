import { describe, it, expect } from 'vitest'
import { buildBody, parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'

const job = {
  id: 'override-job',
  name: 'Override check',
  message: 'Prepare a report.',
  schedule: 'every 1h',
  every_secs: 3600,
  enabled: false,
  channel: 'C0123456789',
  approval_mode: 'auto',
} as CronJob

describe('cron editor overrides', () => {
  it.each([
    ['channel', { channel: '', approval_mode: 'auto' }],
    ['approvalMode', { channel: 'C0123456789', approval_mode: '' }],
  ] as const)('sends an explicit empty value when clearing %s on edit', (field, expected) => {
    const form = { ...parseJobDefaults(job), [field]: '' }
    const body = buildBody(form, 'UTC', () => {}, true)

    expect(body).toMatchObject(expected)
  })

  it('omits unset overrides when creating a job', () => {
    const form = parseJobDefaults({ ...job, channel: '', approval_mode: '' })
    const body = buildBody(form, 'UTC', () => {})

    expect(body).not.toBeNull()
    expect(body).not.toHaveProperty('channel')
    expect(body).not.toHaveProperty('approval_mode')
  })

  it.each([false, true])('preserves explicit overrides when editing=%s', isEdit => {
    const body = buildBody(parseJobDefaults(job), 'UTC', () => {}, isEdit)

    expect(body).toMatchObject({ channel: 'C0123456789', approval_mode: 'auto' })
  })

  it.each([
    { script: 'check.py:run' },
    { command: 'echo check' },
  ])('clears the channel without sending approval for %j', execution => {
    const form = parseJobDefaults({ ...job, ...execution, message: '', channel: '' })
    const body = buildBody(form, 'UTC', () => {}, true)

    expect(body).toMatchObject({ channel: '' })
    expect(body).not.toHaveProperty('approval_mode')
  })
})
