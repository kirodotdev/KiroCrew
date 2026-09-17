/** The project binding is cleared for a job kind whose dispatch never reads it.
 *
 *  `project_path` is rendered only under `!isLlmless`, and a script/command
 *  job's subprocess dispatch derives no cwd from it. Submitting it anyway on a
 *  conversion re-persists a folder the form no longer shows and the run never
 *  uses -- the same reason `chat_folder_id` is cleared there (Opus Review
 *  span=be4da06858f3), and the same way a setting starts lying.
 */

import { buildBody, parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

const makeJob = (over: Partial<CronJob> = {}): CronJob =>
  ({
    id: 'pb1', name: 'bound', message: 'Write the brief.', schedule: '', enabled: true,
    every_secs: 3600, ...over,
  }) as CronJob

describe('project binding on a job kind that cannot use it', () => {
  it('clears project_path when a bound job is converted to a script job', () => {
    const body = buildBody(
      { ...parseJobDefaults(makeJob({ script: 'a.py:run' })), projectPath: '/tmp/proj' },
      'UTC',
      () => {},
      true,
    )
    expect(body?.project_path).toBe('')
  })

  it('still sends the folder for a message job, including the empty clear', () => {
    const bound = buildBody(
      { ...parseJobDefaults(undefined), name: 'n', message: 'm', projectPath: '/tmp/proj' },
      'UTC',
      () => {},
    )
    expect(bound?.project_path).toBe('/tmp/proj')

    // "" is the real value for "unbind", so it must still reach the backend
    // rather than being dropped as falsy.
    const cleared = buildBody(
      { ...parseJobDefaults(makeJob({ project_path: '/tmp/proj' })), projectPath: '' },
      'UTC',
      () => {},
    )
    expect(cleared?.project_path).toBe('')
  })
})
