import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sageApi } from '../apps/code-review-sage/api'

describe('sageApi review model payloads', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        run_id: 'run-1', changes: [], repo: 'acme/widgets', skipped: 0, status: 'started',
      }),
    }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function bodyOf(call = 0) {
    return JSON.parse((fetchMock.mock.calls[call][1] as { body: string }).body)
  }

  it('sends a concrete model for selected PRs', async () => {
    await sageApi.review(['https://github.com/acme/widgets/pull/7'], 'model-concrete')

    expect(bodyOf()).toEqual({
      changes: ['https://github.com/acme/widgets/pull/7'],
      model: 'model-concrete',
    })
  })

  it('omits Auto from selected-PR, pasted-link, and repo-wide requests', async () => {
    await sageApi.review(['https://github.com/acme/widgets/pull/7'], 'auto')
    await sageApi.reviewLinks('https://github.com/acme/widgets/pull/8', 'auto')
    await sageApi.reviewRepo('acme/widgets', false, 'auto')

    expect(bodyOf(0)).toEqual({
      changes: ['https://github.com/acme/widgets/pull/7'],
    })
    expect(bodyOf(1)).toEqual({
      links: 'https://github.com/acme/widgets/pull/8',
    })
    expect(bodyOf(2)).toEqual({ repo: 'acme/widgets', force: false })
  })

  it('sends a concrete model for pasted-link and repo-wide requests', async () => {
    await sageApi.reviewLinks('https://github.com/acme/widgets/pull/8', 'model-concrete')
    await sageApi.reviewRepo('acme/widgets', true, 'model-concrete')

    expect(bodyOf(0)).toEqual({
      links: 'https://github.com/acme/widgets/pull/8',
      model: 'model-concrete',
    })
    expect(bodyOf(1)).toEqual({
      repo: 'acme/widgets', force: true, model: 'model-concrete',
    })
  })
})
