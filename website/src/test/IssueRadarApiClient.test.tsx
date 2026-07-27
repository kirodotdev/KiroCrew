import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// These test the API CLIENT at the fetch boundary, deliberately without mocking
// `issueRadarApi`. The component tests mock the client and synthesize its errors,
// so they stay green if the real request/response translation is reverted — and
// the two things translated here are both silent when they break: a 409 that stops
// becoming a SettingsConflictError turns every cross-tab conflict into a generic
// failure that stops persisting edits, and an empty `numbers` array that gets
// dropped from the body launches a whole automatic batch nobody asked for.
const { issueRadarApi, SettingsConflictError } = await import('../apps/issue-radar/api')

const SETTINGS = {
  triage_labels: ['from-other-tab'],
  unlabeled_is_untriaged: true,
  good_first_issue_labels: [],
  notify_on_new_issue: false,
  revision: 9,
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('issueRadarApi.putSettings', () => {
  it('turns a 409 into SettingsConflictError carrying the server settings', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, {
      error: 'These settings changed in another tab.',
      settings: SETTINGS,
    }))

    await expect(
      issueRadarApi.putSettings('o', 'r', { ...SETTINGS, revision: 4 }),
    ).rejects.toBeInstanceOf(SettingsConflictError)

    // The caller needs the newer document to rebase onto, so it must survive the
    // throw — a generic Error would lose it.
    try {
      await issueRadarApi.putSettings('o', 'r', { ...SETTINGS, revision: 4 })
      throw new Error('expected a conflict')
    } catch (e) {
      expect(e).toBeInstanceOf(SettingsConflictError)
      expect((e as SettingsConflictError).current).toEqual(SETTINGS)
      expect((e as Error).message).toContain('changed in another tab')
    }
  })

  it('still throws a plain Error for other failures', async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { error: 'boom' }))
    const err = await issueRadarApi.putSettings('o', 'r', SETTINGS).catch((e) => e)
    expect(err).toBeInstanceOf(Error)
    expect(err).not.toBeInstanceOf(SettingsConflictError)
    expect((err as Error).message).toBe('boom')
  })

  it('returns the parsed body on success', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { owner: 'o', repo: 'r', settings: SETTINGS }))
    await expect(issueRadarApi.putSettings('o', 'r', SETTINGS))
      .resolves.toEqual({ owner: 'o', repo: 'r', settings: SETTINGS })
  })
})

describe('issueRadarApi.generateTagging', () => {
  const okBody = {
    owner: 'o', repo: 'r', suggestions: {}, analyzed: [], remaining: 0, generated_at: null,
  }

  const sentBody = () => JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)

  it('sends an EXPLICIT empty numbers array', async () => {
    // `[]` means "analyse exactly these (none)". A truthiness check dropped the
    // field, which the backend reads as an omission and answers with a full
    // automatic batch.
    fetchMock.mockResolvedValue(jsonResponse(200, okBody))
    await issueRadarApi.generateTagging('o', 'r', [])
    expect(sentBody()).toEqual({ owner: 'o', repo: 'r', numbers: [] })
  })

  it('omits numbers only when it is undefined', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, okBody))
    await issueRadarApi.generateTagging('o', 'r')
    expect(sentBody()).toEqual({ owner: 'o', repo: 'r' })
  })

  it('passes a populated selection through', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, okBody))
    await issueRadarApi.generateTagging('o', 'r', [7, 8])
    expect(sentBody()).toEqual({ owner: 'o', repo: 'r', numbers: [7, 8] })
  })
})

describe('issueRadarApi.tagging', () => {
  it('only sets refresh when asked', async () => {
    const body = {
      owner: 'o', repo: 'r', issues: [], untagged: [], open_count: 0,
      suggestions: {}, generated_at: null, batch_size: 50, label_counts: {}, titles: {},
    }
    fetchMock.mockResolvedValue(jsonResponse(200, body))
    await issueRadarApi.tagging('o', 'r')
    expect(fetchMock.mock.calls[0][0]).not.toContain('refresh=1')

    fetchMock.mockClear()
    fetchMock.mockResolvedValue(jsonResponse(200, body))
    await issueRadarApi.tagging('o', 'r', { refresh: true })
    expect(fetchMock.mock.calls[0][0]).toContain('refresh=1')
  })
})
