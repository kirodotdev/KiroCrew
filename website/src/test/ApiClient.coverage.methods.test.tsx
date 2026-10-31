/**
 * Behavioural coverage for the shared dashboard API client: the method
 * implementations that are not one-liners (deadlines, the SSE reader, the blob
 * download, the multipart upload) and the sweep that holds EVERY `api` method
 * to one well-formed `/api/...` request.
 *
 * Parts 2 and 3 of the suite `ApiClient.coverage.test.tsx` lays out; the shared
 * fixtures live in `ApiClient.coverageTestUtils.tsx`, and the `vi.mock` block
 * and hooks are the suite's own, word for word.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  api,
  AcceptedBodyUnreadable,
  ApiError,
  __resetAuthRecoveryStateForTests,
  BROWSE_FILES_TIMEOUT_MS,
  FILE_SEARCH_TIMEOUT_MS,
} from '../api/client'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { copyToClipboard } from '../utils/clipboard'
import { resizeImageForModel } from '../utils/resizeImage'
import { __resetArtifactWrites } from '../lib/artifactWrites'
import {
  res,
  okJson,
  fetchMock,
  call,
} from './ApiClient.coverageTestUtils'

// `revealPath` funnels a headless host's `copy` fallback into the clipboard, and
// `uploadFiles` downscales through the canvas helper — neither of which exists
// under happy-dom. Both are stubbed at their module boundary so the client's own
// wiring is what gets asserted.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(async () => {}),
  copyCode: vi.fn(async () => {}),
}))
vi.mock('../utils/resizeImage', () => ({
  MODEL_IMAGE_LIMITS: { maxEdge: 8000, maxBytes: 3_750_000 },
  resizeImageForModel: vi.fn(async (file: File) => ({ file, info: null })),
}))

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue(okJson())
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
  __resetArtifactWrites()
  vi.mocked(copyToClipboard).mockClear()
  vi.mocked(resizeImageForModel).mockClear()
  vi.mocked(resizeImageForModel).mockImplementation(async (file: File) => ({ file, info: null }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

/* ─────────────── 3. the non-trivial method implementations ─────────────── */

describe('deadline-bound endpoints', () => {
  // Exercised against a stubbed `fetch`, the only layer where the bound is
  // observable -- the component harnesses stub `api.*` and would bypass it.

  const realTimeout = globalThis.setTimeout

  /** Shrink the deadline without touching the production composition, and record the
   *  value it asked for, so the assertion costs milliseconds rather than a real 10s
   *  wait. Same shape the base's other client-deadline suites use. */
  function shrinkDeadline(ms: number, record?: (asked: number) => void) {
    vi.spyOn(globalThis, 'setTimeout').mockImplementation(((fn: () => void, asked?: number) => {
      record?.(asked ?? 0)
      return realTimeout(fn, ms)
    }) as unknown as typeof globalThis.setTimeout)
  }

  const wedged = () => fetchMock.mockImplementation(
    (_u: string, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        const s = init?.signal
        if (s?.aborted) return reject(s.reason)
        s?.addEventListener('abort', () => reject(s.reason), { once: true })
      }),
  )

  it('rejects with the TimeoutError-named Error shape the cause-keyed notices decode', async () => {
    // A transport default that rejects with any other shape stops `isDeadlineError` recognising
    // it, silently degrading every cause-keyed notice here to the generic "failed" copy.
    const { isDeadlineError } = await import('../api/queryClient')
    const { searchErrorCause } = await import('../lib/searchErrorCause')
    wedged()
    shrinkDeadline(20)
    const err = await api.browseFiles('/p').then(() => null, (e: unknown) => e)
    expect(err).toBeInstanceOf(Error)
    expect((err as Error).name).toBe('TimeoutError')
    expect(isDeadlineError(err)).toBe(true)
    expect(searchErrorCause(err)).toBe('timed_out')
  })

  it('bounds api.recentProjects, the picker sibling that shares the wedged loop', async () => {
    // Fail-first: unbounded, the Recent tab sat empty for as long as the gateway hung.
    const asked: number[] = []
    wedged()
    shrinkDeadline(20, ms => asked.push(ms))
    await expect(api.recentProjects()).rejects.toMatchObject({ name: 'TimeoutError' })
    expect(asked).toContain(BROWSE_FILES_TIMEOUT_MS)
  })

  it('bounds api.browseDirs too, the sibling both directory pickers spin on', async () => {
    // Fail-first: unbounded, this promise never settled and the Project/Workspace
    // pickers sat on an empty browse list for as long as they stayed open.
    const asked: number[] = []
    wedged()
    shrinkDeadline(20, ms => asked.push(ms))
    await expect(api.browseDirs('/p')).rejects.toMatchObject({ name: 'TimeoutError' })
    expect(asked).toContain(BROWSE_FILES_TIMEOUT_MS)
  })

  it('bounds api.browseFiles, so a wedged gateway stops the folder listing spinning', async () => {
    // Fail-first: unbounded, this promise never settled and FolderPanel showed its
    // loading state for as long as the panel stayed open.
    const asked: number[] = []
    wedged()
    shrinkDeadline(20, ms => asked.push(ms))
    await expect(api.browseFiles('/p')).rejects.toMatchObject({ name: 'TimeoutError' })
    expect(asked).toContain(BROWSE_FILES_TIMEOUT_MS)
  })

  it('bounds api.projectTree by the WHOLE-TREE deadline, not the one-level listing one', async () => {
    // The listing bound is documented as shorter "because a listing walks one level, not the
    // tree", so the tree read sharing it contradicted that; assertion two is the drift control.
    const asked: number[] = []
    wedged()
    shrinkDeadline(20, ms => asked.push(ms))
    await expect(api.projectTree('/p')).rejects.toMatchObject({ name: 'TimeoutError' })
    expect(asked).toContain(FILE_SEARCH_TIMEOUT_MS)
    expect(asked).not.toContain(BROWSE_FILES_TIMEOUT_MS)
  })

  it('leaves api.projectGitStatus UNBOUNDED, the scope this PR declares and its comment states', async () => {
    // Bounding it puts the raw English deadline message into every locale, because the git
    // panel renders the server message verbatim and has no timeout copy of its own.
    const asked: number[] = []
    wedged()
    shrinkDeadline(20, ms => asked.push(ms))
    void api.projectGitStatus('/p').catch(() => {})
    // flake-ok: pre-existing sleep barrier before an absence check, moved verbatim; tracked for a de-flake pass
    await new Promise(resolve => realTimeout(resolve, 60))
    expect(asked).not.toContain(BROWSE_FILES_TIMEOUT_MS)
  })

  it('passes the caller signal through browseFiles, so a superseded listing is cancelled', async () => {
    const ac = new AbortController()
    fetchMock.mockImplementation(
      (_u: string, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true })
        }),
    )
    const p = api.browseFiles('/p', ac.signal)
    ac.abort(new Error('superseded'))
    await expect(p).rejects.toBeTruthy()
  })
})

describe('revealPath', () => {
  // The transport is side-effect-free: it posts the action and returns the wire
  // shape. When the host is headless it hands back a `copy` path for the caller
  // (`revealOrOpen`) to write — `api.revealPath` itself never touches the
  // clipboard, so the degrade lives in exactly one place.
  it('returns the copy path when the host is headless and cannot reveal it', async () => {
    fetchMock.mockResolvedValue(okJson({ copy: '/home/u/report.zip' }))
    const r = await api.revealPath('/home/u/report.zip')
    expect(call().body).toEqual({ path: '/home/u/report.zip', action: 'reveal' })
    expect(r).toMatchObject({ copy: '/home/u/report.zip' })
    expect(vi.mocked(copyToClipboard)).not.toHaveBeenCalled()
  })

  it('does not touch the clipboard when the OS handled it', async () => {
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    const r = await api.revealPath('/f', 'open')
    expect(call().body).toEqual({ path: '/f', action: 'open' })
    expect(vi.mocked(copyToClipboard)).not.toHaveBeenCalled()
    expect(r).toMatchObject({ ok: true })
  })
})

describe('sttTranscribe', () => {
  // happy-dom's FormData does not retain the third (filename) argument — the
  // stored entry is always named `blob` — so the filename the client CHOSE is
  // read off the append call rather than out of the FormData.
  function appendSpy() {
    const calls: unknown[][] = []
    const spy = vi.spyOn(FormData.prototype, 'append').mockImplementation(function (
      this: FormData,
      ...args: unknown[]
    ) {
      calls.push(args)
    })
    return { calls, spy }
  }

  it('uploads the recording as multipart under the field the handler reads', async () => {
    const { calls, spy } = appendSpy()
    const blob = new Blob(['audio'], { type: 'audio/webm' })
    try {
      await api.sttTranscribe(blob, 'wav')
    } finally { spy.mockRestore() }
    const { url, init } = call()
    expect(url).toBe('/api/stt/transcribe')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeInstanceOf(FormData)
    expect(calls).toEqual([['audio', blob, 'recording.wav']])
  })

  it('defaults the container to webm', async () => {
    const { calls, spy } = appendSpy()
    try {
      await api.sttTranscribe(new Blob(['a']))
    } finally { spy.mockRestore() }
    expect(calls[0][2]).toBe('recording.webm')
  })
})

describe('uploadCrewAvatar', () => {
  // Same happy-dom caveat as sttTranscribe: the filename the client chose is
  // read off the append call, not out of the FormData.
  it('stages the picture as multipart under the field the handler reads', async () => {
    const calls: unknown[][] = []
    const spy = vi.spyOn(FormData.prototype, 'append').mockImplementation(function (
      this: FormData,
      ...args: unknown[]
    ) {
      calls.push(args)
    })
    const blob = new Blob(['png'], { type: 'image/png' })
    try {
      await api.uploadCrewAvatar('my crew', blob)
    } finally { spy.mockRestore() }
    const { url, init } = call()
    expect(url).toBe('/api/agents/my%20crew/avatar')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeInstanceOf(FormData)
    expect(calls).toEqual([['file', blob, 'avatar.png']])
  })
})

describe('uploadFiles', () => {
  const png = (name: string) => new File(['x'], name, { type: 'image/png' })

  it('keys resize details by the exact server path the attachment chip renders from', async () => {
    const info = { name: 'big.png', fromW: 4000, fromH: 3000, toW: 1000, toH: 750, fromBytes: 90, toBytes: 9 }
    vi.mocked(resizeImageForModel)
      .mockImplementationOnce(async (f: File) => ({ file: f, info }))
      .mockImplementationOnce(async (f: File) => ({ file: f, info: null }))
    fetchMock.mockResolvedValue(okJson({ paths: ['/up/big.png', '/up/small.png'] }))

    const out = await api.uploadFiles([png('big.png'), png('small.png')])
    expect(out.paths).toEqual(['/up/big.png', '/up/small.png'])
    expect(out.resized).toEqual([info])
    // paths[i] is prepared[i]'s stored location, so the zip must key by path.
    expect(out.resizedByPath).toEqual({ '/up/big.png': info })
    expect(out.error).toBeUndefined()
    expect(call().init?.body).toBeInstanceOf(FormData)
  })

  it('reports the server error and no paths on a failed upload', async () => {
    fetchMock.mockResolvedValue(res(413, { error: 'too large' }))
    const out = await api.uploadFiles([png('a.png')])
    expect(out).toMatchObject({ paths: [], error: 'too large' })
    expect(out.resizedByPath).toEqual({})
  })

  it('falls back to the status text when the failure body is unreadable', async () => {
    fetchMock.mockResolvedValue({
      ok: false, status: 500, statusText: 'Internal Server Error',
      headers: { get: () => null },
      json: async () => { throw new Error('not json') },
    } as unknown as Response)
    const out = await api.uploadFiles([png('a.png')])
    expect(out).toMatchObject({ paths: [], error: 'Internal Server Error' })
  })

  it("hands the caller's AbortSignal to the request, so an upload can be cancelled", async () => {
    const ac = new AbortController()
    fetchMock.mockResolvedValue(okJson({ paths: ['/up/a.png'] }))
    await api.uploadFiles([png('a.png')], ac.signal)
    // Without this the composer's cancel control has nothing to abort: the
    // request runs to completion whatever the user does.
    expect(call().init?.signal).toBe(ac.signal)
  })

  it('omits signal entirely when the caller passes none', async () => {
    fetchMock.mockResolvedValue(okJson({ paths: ['/up/a.png'] }))
    await api.uploadFiles([png('a.png')])
    expect(call().init?.signal).toBeUndefined()
  })

  it('refuses to trust a 200 whose paths field is not an array', async () => {
    fetchMock.mockResolvedValue(okJson({ paths: 'oops' }))
    const out = await api.uploadFiles([png('a.png')])
    expect(out.paths).toEqual([])
    expect(out.error).toBeTruthy()
  })
})

describe('exportPlanYaml', () => {
  let clicks: { download: string; href: string }[]
  let createObjectURL: typeof URL.createObjectURL
  let revokeObjectURL: typeof URL.revokeObjectURL
  let clickSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    clicks = []
    createObjectURL = URL.createObjectURL
    revokeObjectURL = URL.revokeObjectURL
    URL.createObjectURL = vi.fn(() => 'blob:plan')
    URL.revokeObjectURL = vi.fn()
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push({ download: this.download, href: this.getAttribute('href') ?? '' })
    })
  })
  afterEach(() => {
    clickSpy.mockRestore()
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL
  })

  it('honours the sanitized filename the server sent', async () => {
    fetchMock.mockResolvedValue(
      res(200, 'steps: []', { headers: { 'Content-Disposition': 'attachment; filename="my plan.yaml"' } }),
    )
    await api.exportPlanYaml('task 1')
    expect(call().url).toBe('/api/taskrunner/task%201/plan.yaml')
    expect(clicks).toEqual([{ download: 'my plan.yaml', href: 'blob:plan' }])
    // The object URL is released, and the anchor is not left in the document.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:plan')
    expect(document.querySelector('a[download]')).toBeNull()
  })

  it('falls back to <taskId>.yaml when no Content-Disposition arrives', async () => {
    fetchMock.mockResolvedValue(res(200, 'steps: []'))
    await api.exportPlanYaml('t1')
    expect(clicks[0].download).toBe('t1.yaml')
  })

  it('throws an ApiError instead of downloading an error page', async () => {
    fetchMock.mockResolvedValue(res(404, 'no such run'))
    await expect(api.exportPlanYaml('t1')).rejects.toBeInstanceOf(ApiError)
    expect(clicks).toEqual([])
  })

  it('reports the bare status when the failure body is empty', async () => {
    fetchMock.mockResolvedValue(res(500, '', { text: '' }))
    await expect(api.exportPlanYaml('t1')).rejects.toThrow('HTTP 500')
  })
})

describe('installFromRegistryStream', () => {
  /** A ReadableStream-shaped stub that hands back the given chunks in order. */
  function sseBody(chunks: string[]) {
    const enc = new TextEncoder()
    let i = 0
    const releaseLock = vi.fn()
    return {
      body: {
        getReader: () => ({
          read: async () =>
            i < chunks.length
              ? { done: false, value: enc.encode(chunks[i++]) }
              : { done: true, value: undefined },
          releaseLock,
        }),
      },
      releaseLock,
    }
  }

  function streamRes(chunks: string[]) {
    const { body, releaseLock } = sseBody(chunks)
    return { response: res(200, '') as unknown as Record<string, unknown>, body, releaseLock }
  }

  it('streams every log line and resolves with the done payload', async () => {
    const { response, body, releaseLock } = streamRes([
      'event: log\ndata: cloning\n\nevent: log\ndata: build',
      'ing\n\nevent: done\ndata: {"ok":true}\n\n',
    ])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const logs: string[] = []
    const out = await api.installFromRegistryStream('demo', (l) => logs.push(l))
    expect(logs).toEqual(['cloning', 'building'])
    expect(out).toEqual({ ok: true })
    expect(call().body).toEqual({ name: 'demo' })
    expect(call().headers['Content-Type']).toBe('application/json')
    expect(releaseLock).toHaveBeenCalled()
  })

  it('joins a multi-line data payload and tolerates a bare `data:` line', async () => {
    const { response, body } = streamRes(['event: log\ndata: one\ndata:\ndata: three\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const logs: string[] = []
    await api.installFromRegistryStream('demo', (l) => logs.push(l))
    expect(logs).toEqual(['one\n\nthree'])
  })

  it('carries the refusal code so the consent modal can open on a resolved refusal', async () => {
    // The execution gate refuses BEFORE cloning and RESOLVES (SSE done) rather
    // than rejecting, so the code has to travel on the result.
    const { response, body } = streamRes([
      'event: done\ndata: {"ok":false,"code":"app_execution_denied"}\n\n',
    ])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const out = await api.installFromRegistryStream('demo', () => {})
    expect(out).toEqual({ ok: false, code: 'app_execution_denied' })
  })

  it('degrades a malformed done payload into a failure carrying the raw text', async () => {
    const { response, body } = streamRes(['event: done\ndata: not-json\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    await expect(api.installFromRegistryStream('demo', () => {})).resolves.toEqual({
      ok: false, error: 'not-json',
    })
  })

  it('fails when the stream ends without a done frame', async () => {
    const { response, body } = streamRes(['event: log\ndata: half\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const out = await api.installFromRegistryStream('demo', () => {})
    expect(out.ok).toBe(false)
    expect(out.error).toBeTruthy()
  })

  it('skips blank frames rather than emitting empty log lines', async () => {
    const { response, body } = streamRes(['\n\n   \n\nevent: done\ndata: {"ok":true}\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const logs: string[] = []
    await api.installFromRegistryStream('demo', (l) => logs.push(l))
    expect(logs).toEqual([])
  })

  it('throws before reading when the request itself is rejected', async () => {
    fetchMock.mockResolvedValue(res(500, 'gateway said no'))
    await expect(api.installFromRegistryStream('demo', () => {})).rejects.toThrow('gateway said no')
  })

  it('forwards the abort signal', async () => {
    const ctl = new AbortController()
    const { response, body } = streamRes(['event: done\ndata: {"ok":true}\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    await api.installFromRegistryStream('demo', () => {}, ctl.signal)
    expect(call().init?.signal).toBe(ctl.signal)
  })
})

describe('publishToProvider', () => {
  it('routes to the provider-declared endpoint with the deploy payload shape', async () => {
    fetchMock.mockResolvedValue(okJson({ url: 'https://x' }))
    await api.publishToProvider('my-site', 'aws', {
      id: 'aws', label: 'AWS', icon: '', kinds: ['webapp'],
      configured: true, setupRoute: '/s', endpoint: '/api/deploy/custom',
    })
    expect(call().url).toBe('/api/deploy/custom')
    expect(call().body).toEqual({ site_id: 'my-site', artifact_slug: 'my-site', provider_id: 'aws' })
  })

  it('defaults the endpoint when the provider descriptor is unavailable', async () => {
    fetchMock.mockResolvedValue(okJson({ url: 'https://x' }))
    await api.publishToProvider('my-site', 'aws')
    expect(call().url).toBe('/api/deploy/deploy')
  })

  it('sends ttl_hours so the previewed TTL matches what is deployed', async () => {
    fetchMock.mockResolvedValue(okJson({ url: 'https://x' }))
    await api.publishToProvider('my-site', 'aws', undefined, 24)
    expect(call().body).toMatchObject({ ttl_hours: 24 })
  })

  it('returns a 409 scan-block body instead of throwing, so the findings can render', async () => {
    fetchMock.mockResolvedValue(res(409, { findings: [{ rule: 'secret' }] }))
    await expect(api.publishToProvider('s', 'aws')).resolves.toEqual({ findings: [{ rule: 'secret' }] })
  })

  it('throws an ApiError on any other failure', async () => {
    fetchMock.mockResolvedValue(res(500, 'deploy exploded'))
    await expect(api.publishToProvider('s', 'aws')).rejects.toBeInstanceOf(ApiError)
  })

  it('reports the bare status when a failure carries no body', async () => {
    fetchMock.mockResolvedValue(res(502, '', { text: '' }))
    await expect(api.publishToProvider('s', 'aws')).rejects.toThrow('HTTP 502')
  })
})

describe('voice synthesis request ownership', () => {
  it('announces the request synchronously and sends the same id to the gateway', async () => {
    const started = vi.fn()
    window.addEventListener('voice-synthesis-start', started)
    try {
      const synthesis = api.voiceSynthesize('slot-1', '你好')
      expect(started).toHaveBeenCalledOnce()
      const detail = (started.mock.calls[0][0] as CustomEvent).detail
      expect(detail.slot).toBe('slot-1')
      expect(detail.request_id).toEqual(expect.any(String))
      await synthesis
      expect(call().body).toEqual({ slot: 'slot-1', text: '你好', request_id: detail.request_id })
    } finally {
      window.removeEventListener('voice-synthesis-start', started)
    }
  })

  it('preserves the structured HTTP error for the request owner to filter', async () => {
    const failed = vi.fn()
    fetchMock.mockResolvedValue(res(502, { error: 'provider unavailable', code: 'voice_model_config_invalid' }))
    window.addEventListener('voice-synthesis-failed', failed)
    try {
      await expect(api.voiceSynthesize('slot-1', '你好', { request_id: 'request-1' })).rejects.toBeInstanceOf(ApiError)
      expect(failed).toHaveBeenCalledOnce()
      expect((failed.mock.calls[0][0] as CustomEvent).detail).toEqual({
        slot: 'slot-1', request_id: 'request-1', code: 'voice_model_config_invalid',
      })
    } finally {
      window.removeEventListener('voice-synthesis-failed', failed)
    }
  })
})

/* ─────────────────── 4. whole-surface request invariant ─────────────────── */

describe('every api method issues one well-formed /api request', () => {
  // Exercised individually above with the fixtures they need (a Blob, a
  // ReadableStream, a File list, an object-URL download).
  // `skills` joins them because it wraps the fetch in a deadline, so it USES the
  // signal argument rather than forwarding it; junk there is not a URL question.
  // `appSessionStatus` cannot be probed positionally: its 4th argument is a
  // boolean selecting between the two prefixes an app backend may be served at,
  // and the generic probe passes the truthy string 'sw-4', which legitimately
  // routes to the reverse-proxy base `/apps/<app>/api/` rather than `/api/`.
  // Covered exhaustively by `appSessionStatus.test.ts` — both prefixes, app-name
  // encoding, the statusPath guard, and the junk-leak assertions this table
  // would otherwise apply.
  // `wakatimeExportUrl` is a pure URL builder: it RETURNS the string the export
  // anchor navigates to and issues no fetch, so the one-request probe below
  // cannot apply. Covered by a direct URL assertion in `query-string builders`
  // (`ApiClient.coverage.construction.test.tsx`).
  const HAND_TESTED = new Set(['sttTranscribe', 'uploadFiles', 'uploadCrewAvatar', 'installFromRegistryStream', 'exportPlanYaml', 'skills', 'slashCommands', 'appSessionStatus', 'wakatimeExportUrl'])

  type AnyFn = (...args: unknown[]) => unknown
  const methods = Object.entries(api as unknown as Record<string, AnyFn>)
    .filter(([name, fn]) => typeof fn === 'function' && !HAND_TESTED.has(name))

  // A deadline composes its own signal with the caller's, so identity is not the
  // contract for these methods. Keep this list narrow: every other dispatched
  // signal must still be the exact one supplied by the caller.
  const DEADLINE_WRAPPED_METHODS = new Set([
    'fileSearch',
    'browseFiles',
    'browseDirs',
    'browseDrives',
    'recentProjects',
    'favoriteProjects',
    'projectAwsProfile',
    'projectTree',
  ])
  const CALLER_SIGNAL = new AbortController().signal

  // Methods whose URL comes out of an ARGUMENT'S FIELD rather than a positional
  // string. The generic `'sw-1'` args below would make such a method build its
  // URL from `undefined` — a harness artifact, not a defect in the method — so
  // each one names the minimal shape its URL is read from. Signal-bearing methods
  // also receive a real signal so the probe can assert their dispatch contract.
  const ARGS: Record<string, unknown[]> = {
    // Memory reads take typed objects; positional strings do not satisfy the
    // query/record contract and would manufacture undefined URL parameters.
    memoryRecords: ['member-reviewer', { q: 'contact', kind: 'fact' }, 0, 50],
    memoryRecordHistory: ['member-reviewer', { kind: 'fact', id: 'user.contact' }, 25, 0],
    // `invokeFileMenuItem(item, ctx)`: the URL is `item.endpoint`.
    invokeFileMenuItem: [
      { id: 'send', app: 'doc-store', endpoint: '/api/apps/doc-store/send' },
      { surface: 'file-overflow', path: '/tmp/a.txt', kind: 'file' },
    ],
    // `importSessionFromFile(file)` posts the file's BYTES as the request body,
    // deliberately un-wrapped. The generic `'sw-1'` would arrive as a STRING
    // body, which `call()` then tries to JSON-parse -- a harness artifact,
    // not a defect: a real caller hands this a Blob, exactly as here, and a Blob
    // body is left alone the same way a FormData one is.
    importSessionFromFile: [new Blob(['{}'], { type: 'application/gzip' })],
    // `installThemeZip(file)` posts the archive's bytes the same way.
    installThemeZip: [new Blob(['PK'], { type: 'application/zip' })],
    updateInstance: ['sw-1', {}, { signal: CALLER_SIGNAL }],
    chatSlotDetail: ['sw-1', undefined, undefined, CALLER_SIGNAL],
    // The second argument is the list of routes the dashboard cannot open; a
    // bare 'sw-2' string is not a list.
    featureVideoNext: ['sw-1', ['/members']],
    sendChat: ['sw-1', 'sw-2', 'sw-3', CALLER_SIGNAL],
    spawnStatus: ['sw-1', { signal: CALLER_SIGNAL }],
    fileSearch: ['sw-1', 'sw-2', CALLER_SIGNAL],
    pathComplete: ['sw-1', 'sw-2', 'sw-3', CALLER_SIGNAL],
    browseFiles: ['sw-1', CALLER_SIGNAL],
    // The creator filter takes one query object (server-side filter and page).
    sharedCrewSessions: [{ creator: 'mine', q: 'sw-1', cursor: 'sw-2', limit: 50 }],
    sharedCrewCancelWork: ['sw-1', 3],
  }

  it('covers the whole surface (guards against the table silently shrinking)', () => {
    expect(methods.length).toBeGreaterThan(300)
  })

  it('keeps deadline-bound file reads in the universal request probe', () => {
    expect(methods.map(([name]) => name)).toEqual(expect.arrayContaining([
      'fileSearch',
      'browseFiles',
      'browseDirs',
      'recentProjects',
      'favoriteProjects',
    ]))
  })

  it.each(methods.map(([name]) => name))('%s', async (name) => {
    const fn = (api as unknown as Record<string, AnyFn>)[name]
    // Generic positional arguments. Every method is a thin URL/body builder, so
    // what this asserts is the construction itself: a forgotten argument or a
    // botched template literal shows up as `undefined` inside the path.
    await Promise.resolve(fn(...(ARGS[name] ?? ['sw-1', 'sw-2', 'sw-3', 'sw-4'])))

    const expectedRequests = name === 'createChatSlot' ? 2 : 1
    expect(fetchMock, `${name} issued the wrong request count`).toHaveBeenCalledTimes(expectedRequests)
    const { url, init } = call(expectedRequests - 1)
    expect(typeof url, `${name} did not pass a string URL`).toBe('string')
    expect(url.startsWith('/api/'), `${name} escaped the /api prefix: ${url}`).toBe(true)
    for (const junk of ['undefined', '[object Object]', 'NaN', '/null']) {
      expect(url.includes(junk), `${name} leaked ${junk} into ${url}`).toBe(false)
    }
    if (init?.signal !== undefined) {
      if (DEADLINE_WRAPPED_METHODS.has(name)) {
        expect(init.signal, `${name} dropped its deadline signal`).toBeInstanceOf(AbortSignal)
      } else {
        expect(init.signal, `${name} did not forward the caller signal`).toBe(CALLER_SIGNAL)
      }
    }
    // A body is only ever sent with a method that can carry one.
    if (init?.body !== undefined) {
      expect(['POST', 'PUT', 'PATCH', 'DELETE']).toContain(init.method)
    }
  })
})

describe('api.sideTurn keeps the response phases distinguishable', () => {
  // `/side/turn` 2xx is an acceptance receipt: the turn is already running. A
  // body-read failure after it must not look like a request that never left
  // (both are `TypeError`s), or the caller would offer a retry that runs the
  // turn twice.
  it('a 2xx whose body read fails rejects with AcceptedBodyUnreadable carrying the cause', async () => {
    const cause = new TypeError('network error')
    fetchMock.mockResolvedValueOnce({
      ...okJson(),
      json: async () => { throw cause },
    } as unknown as Response)
    await expect(api.sideTurn('s1', 'q')).rejects.toSatisfy(
      (e: unknown) => e instanceof AcceptedBodyUnreadable && e.reason === cause,
    )
  })

  it('a request that never left rejects with the raw TypeError', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(api.sideTurn('s1', 'q')).rejects.toBeInstanceOf(TypeError)
  })

  it('a non-2xx still rejects with ApiError (auth handling and friendly text unchanged)', async () => {
    fetchMock.mockResolvedValueOnce(res(409, { error: 'side turn already in flight' }))
    await expect(api.sideTurn('s1', 'q')).rejects.toBeInstanceOf(ApiError)
  })

  it('a non-2xx whose body read fails STILL rejects with ApiError (status is the verdict; the body was only its explanation)', async () => {
    // A refusal must never degrade into a raw network rejection: the
    // acceptance-receipt wires treat those as indeterminate ("delivery
    // unconfirmed"), which would hide a real refusal from the error surface.
    fetchMock.mockResolvedValueOnce({
      ...res(409, ''),
      statusText: 'Conflict',
      text: async () => { throw new TypeError('body stream cut') },
    } as unknown as Response)
    await expect(api.sideTurn('s1', 'q')).rejects.toSatisfy(
      (e: unknown) => e instanceof ApiError && e.status === 409,
    )
  })
})
