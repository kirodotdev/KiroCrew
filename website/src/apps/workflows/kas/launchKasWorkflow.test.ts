import { describe, expect, it, vi } from 'vitest'

import { cleanInputs, kasChatTitle, launchKasWorkflow, type KasLaunchSteps } from './launchKasWorkflow'

/** Steps that record every call, in order, into one shared log. */
function recordingSteps(overrides: Partial<KasLaunchSteps> = {}) {
  const calls: [string, unknown][] = []
  const steps: KasLaunchSteps = {
    ensureFolder: vi.fn(async () => {
      calls.push(['ensureFolder', null])
      return 'folder-wf'
    }),
    createChat: vi.fn(async opts => {
      calls.push(['createChat', opts])
      return 'chat-new'
    }),
    launch: vi.fn(async body => {
      calls.push(['launch', body])
      return { slot: body.slot, queued: false }
    }),
    discardChat: vi.fn(async slot => {
      calls.push(['discardChat', slot])
    }),
    ...overrides,
  }
  return { steps, calls }
}

const RECIPE = { name: 'Review and fix', source: 'opaque:review-fix' }

describe('launchKasWorkflow', () => {
  it('finds the folder, creates the titled chat with its project, then launches in it', async () => {
    const { steps, calls } = recordingSteps()
    const result = await launchKasWorkflow(steps, {
      recipe: RECIPE,
      project: ' /work/proj ',
      inputs: { target: 'src', empty: '  ' },
    })
    expect(result).toEqual({ slot: 'chat-new', queued: false })
    expect(calls).toEqual([
      ['ensureFolder', null],
      ['createChat', { title: 'Workflow: Review and fix', folderId: 'folder-wf', project: '/work/proj' }],
      ['launch', { slot: 'chat-new', source: 'opaque:review-fix', inputs: { target: 'src' }, name: 'Review and fix' }],
    ])
    expect(steps.discardChat).not.toHaveBeenCalled()
  })

  it('passes the recipe source back verbatim and omits inputs when none were filled', async () => {
    const { steps } = recordingSteps()
    await launchKasWorkflow(steps, { recipe: RECIPE, project: '/p', inputs: { a: '' } })
    expect(steps.launch).toHaveBeenCalledWith({ slot: 'chat-new', source: 'opaque:review-fix', name: 'Review and fix' })
  })

  it('removes the chat it created when the launch is refused, and surfaces the refusal', async () => {
    const refusal = new Error('engine_not_kas')
    const { steps, calls } = recordingSteps({ launch: vi.fn(async () => { throw refusal }) })
    await expect(launchKasWorkflow(steps, { recipe: RECIPE, project: '/p' })).rejects.toBe(refusal)
    expect(calls.at(-1)).toEqual(['discardChat', 'chat-new'])
  })

  it('still surfaces the refusal when cleaning up the chat fails too', async () => {
    const refusal = new Error('bad source')
    const { steps } = recordingSteps({
      launch: vi.fn(async () => { throw refusal }),
      discardChat: vi.fn(async () => { throw new Error('delete failed') }),
    })
    await expect(launchKasWorkflow(steps, { recipe: RECIPE, project: '/p' })).rejects.toBe(refusal)
  })

  it('creates nothing without a project', async () => {
    const { steps } = recordingSteps()
    await expect(launchKasWorkflow(steps, { recipe: RECIPE, project: '   ' })).rejects.toThrow()
    expect(steps.ensureFolder).not.toHaveBeenCalled()
    expect(steps.createChat).not.toHaveBeenCalled()
  })

  it('does not launch when the chat cannot be created', async () => {
    const { steps } = recordingSteps({ createChat: vi.fn(async () => { throw new Error('project refused') }) })
    await expect(launchKasWorkflow(steps, { recipe: RECIPE, project: '/p' })).rejects.toThrow('project refused')
    expect(steps.launch).not.toHaveBeenCalled()
    expect(steps.discardChat).not.toHaveBeenCalled()
  })
})

describe('launch helpers', () => {
  it('titles the chat after the recipe', () => {
    expect(kasChatTitle('Ship it')).toBe('Workflow: Ship it')
  })

  it('drops blank inputs', () => {
    expect(cleanInputs({ a: 'x', b: ' ' })).toEqual({ a: 'x' })
    expect(cleanInputs({ b: '' })).toBeUndefined()
    expect(cleanInputs(undefined)).toBeUndefined()
  })

  it('creates nothing for a recipe KAS listed without a source (gap 18)', async () => {
    const { steps } = recordingSteps()
    await expect(
      launchKasWorkflow(steps, { recipe: { name: 'broken', source: null }, project: '/p' }),
    ).rejects.toThrow()
    expect(steps.ensureFolder).not.toHaveBeenCalled()
    expect(steps.createChat).not.toHaveBeenCalled()
  })
})

describe('launchKasWorkflow — launch options', () => {
  it('sends the display name, model and effort, and never a creator effort', async () => {
    const { steps, calls } = recordingSteps()
    await launchKasWorkflow(steps, { recipe: RECIPE, project: '/p', name: ' Nightly fix ', model: 'claude-sonnet-4.5', effort: 'high' })
    expect(calls[1]).toEqual(['createChat', { title: 'Workflow: Nightly fix', folderId: 'folder-wf', project: '/p' }])
    const body = calls[2][1] as Record<string, unknown>
    expect(body).toMatchObject({ name: 'Nightly fix', runLabel: 'Nightly fix', model: 'claude-sonnet-4.5', effort: 'high' })
    expect(body).not.toHaveProperty('creatorEffort')
  })

  it('blank options fall back to the recipe name and each step’s own model and effort', async () => {
    const { steps, calls } = recordingSteps()
    await launchKasWorkflow(steps, { recipe: RECIPE, project: '/p', name: '  ', model: '', effort: '' })
    const body = calls[2][1] as Record<string, unknown>
    expect(body.name).toBe('Review and fix')
    // No typed name: Kiro Agent keeps the recipe's own name for the run.
    expect(body).not.toHaveProperty('runLabel')
    expect(body).not.toHaveProperty('model')
    expect(body).not.toHaveProperty('effort')
  })
})
