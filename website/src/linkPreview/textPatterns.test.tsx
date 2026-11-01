import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { createElement } from 'react'
import { render, waitFor, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MarkdownRenderer from '../components/MarkdownRenderer'
import {
  MAX_CACHED_TEXTS,
  SCAN_BUDGET_MS,
  WORKER_START_TIMEOUT_MS,
  hasTextPatterns,
  resetTextPatternsForTest,
  resolveTextPreview,
  setLinkPreviewTextPatterns,
  setTextPatternWorkerFactoryForTest,
  setTextPatternsOff,
  textPatternGeneration,
  textPatternHits,
} from './textPatterns'
import { scanTexts, MAX_SCAN_SUBJECT, type FromWorker, type ToWorker } from './textPatternScan'
import { resolveLinkPreviews } from './linkPreviewRegistry'
import { registerAutolinkRules, resetAutolinkRulesForTest } from '../utils/autolinkRules'
import { framesIn, fromFrame, installFrameWindows, sentTo } from '../test/LinkPreviewFramesTestUtils'

installFrameWindows()

const listApps = vi.fn()
vi.mock('../api/client', () => ({ api: { listApps: () => listApps() } }))

const TICKET = { id: 'ticket', pattern: '(?<![\\w-])(TK-\\d{4,})(?![\\w-])' }
const PERSON = { id: 'person', pattern: '(?<![\\w.@])([a-z][a-z0-9]{1,11})@(?![\\w-])' }
const OWNER = { owner: 'cards-app:cards' }
/** Exponential backtracking on `TK-` + digits with no match; the fake worker hangs on it. */
const CATASTROPHIC = '\\bTK-(\\d+)+$'
const CATASTROPHIC_2 = '^(a|aa)+$'

/**
 * An in-thread stand-in for the scan worker (vitest has no `Worker`): it runs the
 * same `scanTexts` and answers on a microtask. A pattern listed in `hangOn` makes
 * it post `start` for that pattern and never answer, as a real worker stuck in one
 * `exec` would; `terminate` is recorded.
 */
function fakeWorkers(
  hangOn: string[] | ((source: string, texts: readonly string[]) => boolean) = [],
  takesMs: Record<string, number> = {},
) {
  const hangs = (source: string, texts: readonly string[]) =>
    typeof hangOn === 'function' ? hangOn(source, texts) : hangOn.includes(source)
  const made: Array<{ terminated: boolean; scans: ToWorker[] }> = []
  const factory = () => {
    const rec = { terminated: false, scans: [] as ToWorker[] }
    made.push(rec)
    const w = {
      onmessage: null as ((e: MessageEvent<FromWorker>) => void) | null,
      terminate() { rec.terminated = true },
      postMessage(msg: ToWorker) {
        rec.scans.push(msg)
        const send = (m: FromWorker) => queueMicrotask(() => { if (!rec.terminated) w.onmessage?.({ data: m } as MessageEvent<FromWorker>) })
        // Patterns run in order, each announced first, as the real worker does.
        const run = (i: number): void => {
          if (rec.terminated) return
          if (i === msg.patterns.length) {
            const rejected: number[] = []
            const hits = scanTexts(msg.patterns, msg.texts, () => {}, (j) => rejected.push(j))
            send({ type: 'done', batch: msg.batch, hits, rejected })
            return
          }
          send({ type: 'pattern-start', batch: msg.batch, i })
          const source = msg.patterns[i].source
          if (hangs(source, msg.texts)) return
          const ms = takesMs[source]
          if (ms) setTimeout(() => run(i + 1), ms)
          else queueMicrotask(() => run(i + 1))
        }
        run(0)
      },
    }
    return w
  }
  return { factory, made }
}

function app(textPatterns: unknown, hosts = ['tickets.example.com', 'people.example.com']) {
  return {
    name: 'cards-app',
    enabled: true,
    manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/lp.mjs', hosts, textPatterns }] } },
  }
}
const spans = (c: HTMLElement) => [...c.querySelectorAll('[data-link-preview-text]')].map((s) => s.textContent)
const use = (patterns: unknown, hosts?: string[]) => setLinkPreviewTextPatterns(resolveLinkPreviews([app(patterns, hosts)]))
/** Let the queued batch reach the worker, the answer come back, and the message re-render. */
const settle = async () => { await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }) }

beforeEach(() => setTextPatternWorkerFactoryForTest(fakeWorkers().factory))
afterEach(() => {
  setTextPatternWorkerFactoryForTest()
  resetTextPatternsForTest()
  resetAutolinkRulesForTest()
  listApps.mockReset()
  document.body.replaceChildren()
  vi.useRealTimers()
  // A failing test must not leave its console spy on the next one.
  vi.restoreAllMocks()
})

describe('textPatternScan (the worker side)', () => {
  it('finds non-overlapping whole matches, earlier patterns first, and skips oversized text', () => {
    const [hits, none] = scanTexts([{ source: TICKET.pattern, flags: 'g' }, { source: 'TK-\\d+', flags: '' }], ['see TK-1234 and TK-5678', 'x'.repeat(MAX_SCAN_SUBJECT + 1)])
    expect(hits).toEqual([{ start: 4, end: 11, pattern: 0 }, { start: 16, end: 23, pattern: 0 }])
    expect(none).toEqual([])
  })

  it("names each hit's owner as the first pattern that takes its whole text", () => {
    const [hits] = scanTexts([{ source: '^TK-\\d+$', flags: '' }, { source: 'TK-\\d+', flags: '' }], ['see TK-1234'])
    expect(hits).toEqual([{ start: 4, end: 11, pattern: 0 }])
  })

  it('reports a pattern that matches the empty string instead of scanning with it', () => {
    const rejected: number[] = []
    const [hits] = scanTexts([{ source: 'x*', flags: '' }, { source: 'TK-\\d+', flags: '' }], ['TK-1 xx'], () => {}, (i) => rejected.push(i))
    expect(rejected).toEqual([0])
    expect(hits).toEqual([{ start: 0, end: 4, pattern: 1 }])
  })

  it('drops a hit its pattern would not match on its own (a positive lookbehind)', () => {
    expect(scanTexts([{ source: '(?<=ticket )\\d{4}', flags: '' }], ['see ticket 1234'])).toEqual([[]])
  })
})

describe('textPatterns: the runtime budget', () => {
  it('a pattern that overruns the budget is terminated, disabled for the session and logged once; the rest still match', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const workers = fakeWorkers([CATASTROPHIC, CATASTROPHIC_2])
    setTextPatternWorkerFactoryForTest(workers.factory)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    use([{ id: 'bad', pattern: CATASTROPHIC }, { id: 'bad2', pattern: CATASTROPHIC_2 }, TICKET])
    const { container } = render(<MarkdownRenderer textPreviews content={`${'a'.repeat(40)}! and TK-1234`} />)
    await settle()
    expect(spans(container)).toEqual([]) // the first worker is stuck in the first bad pattern
    await act(async () => { vi.advanceTimersByTime(SCAN_BUDGET_MS + 1) })
    await settle()
    expect(workers.made[0].terminated).toBe(true)
    // The retry runs without the first bad pattern and gets stuck in the second.
    expect(workers.made[1].scans[0].patterns.map((p) => p.source)).toEqual([CATASTROPHIC_2, TICKET.pattern])
    await act(async () => { vi.advanceTimersByTime(SCAN_BUDGET_MS + 1) })
    await settle()
    expect(workers.made[1].terminated).toBe(true)
    // A fresh worker scanned the same text with only the healthy pattern.
    expect(workers.made[2].scans[0].patterns.map((p) => p.source)).toEqual([TICKET.pattern])
    expect(spans(container)).toEqual(['TK-1234'])
    // Later text never sends a disabled pattern again, and the overruns were logged once.
    render(<MarkdownRenderer textPreviews content="TK-9876 again" />)
    await settle()
    expect(workers.made.flatMap((w) => w.scans).slice(2).every((s) => s.patterns.every((p) => p.source === TICKET.pattern))).toBe(true)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('cards-app:cards')
    warn.mockRestore()
  })

  it('a streaming reply queues nothing; its final text is scanned once', async () => {
    // Each animation frame of a streaming reply is a new, longer tail text. Queued,
    // they would fill the bounded cache and silently end plain-text previews.
    const workers = fakeWorkers()
    setTextPatternWorkerFactoryForTest(workers.factory)
    use([TICKET])
    const { container, rerender } = render(<MarkdownRenderer textPreviews textPreviewsPaused content="see TK-1" />)
    for (const tail of ['see TK-12', 'see TK-123', 'see TK-1234 and']) {
      rerender(<MarkdownRenderer textPreviews textPreviewsPaused content={tail} />)
      await settle()
    }
    expect(workers.made.flatMap((w) => w.scans)).toEqual([])
    // Links in it are still in a chat message's scope while it streams.
    expect(container.querySelector('[data-link-preview-scope]')).not.toBeNull()
    rerender(<MarkdownRenderer textPreviews content="see TK-1234 and" />)
    await settle()
    expect(workers.made.flatMap((w) => w.scans.flatMap((s) => s.texts))).toEqual(['see TK-1234 and'])
    expect(spans(container)).toEqual(['TK-1234'])
  })

  it('a fast pattern followed by a catastrophic one: only the catastrophic one is disabled', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const workers = fakeWorkers([CATASTROPHIC])
    setTextPatternWorkerFactoryForTest(workers.factory)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    use([TICKET, { id: 'bad', pattern: CATASTROPHIC }])
    const { container } = render(<MarkdownRenderer textPreviews content="see TK-1234 here" />)
    await settle()
    await act(async () => { vi.advanceTimersByTime(SCAN_BUDGET_MS + 1) })
    await settle()
    expect(workers.made[0].terminated).toBe(true)
    // The retry runs only the fast pattern, and it still matches.
    expect(workers.made[1].scans[0].patterns.map((p) => p.source)).toEqual([TICKET.pattern])
    expect(spans(container)).toEqual(['TK-1234'])
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('an overrun drops cached owners: text the disabled pattern owned is rescanned and goes to the next owner', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const long = `TK-${'1'.repeat(40)}!`
    // The catastrophic pattern hangs only on the long text; on short text it matches.
    const workers = fakeWorkers((source, texts) => source === CATASTROPHIC && texts.includes(long))
    setTextPatternWorkerFactoryForTest(workers.factory)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const a = { ...app([{ id: 'slow', pattern: CATASTROPHIC }, { id: 'p', pattern: 'P-\\d+' }]), name: 'a-app' }
    const b = { ...app([{ id: 'tk', pattern: 'TK-\\d+' }]), name: 'b-app' }
    setLinkPreviewTextPatterns(resolveLinkPreviews([a, b]))
    const first = render(<MarkdownRenderer textPreviews content="see TK-1234" />)
    await settle()
    expect(spans(first.container)).toEqual(['TK-1234'])
    expect(resolveTextPreview('TK-1234')).toEqual({ owner: 'a-app:cards' })
    render(<MarkdownRenderer textPreviews content={long} />)
    await settle()
    await act(async () => { vi.advanceTimersByTime(SCAN_BUDGET_MS + 1) })
    await settle()
    // A's slow pattern is off. The cached owner of TK-1234 was A's; it is not kept,
    // although A's other pattern is still active.
    expect(resolveTextPreview('TK-1234')).toEqual({ owner: 'b-app:cards' })
    expect(spans(first.container)).toEqual(['TK-1234'])
    warn.mockRestore()
  })

  it('each pattern has its own budget: two that each fit are both kept, though together they exceed one', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const SLOW = { id: 'slow', pattern: 'S-\\d+' }
    const part = Math.round(SCAN_BUDGET_MS * 0.8)
    const workers = fakeWorkers([], { [SLOW.pattern]: part, [TICKET.pattern]: part })
    setTextPatternWorkerFactoryForTest(workers.factory)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    use([SLOW, TICKET])
    const { container } = render(<MarkdownRenderer textPreviews content="S-1 and TK-1234" />)
    await settle()
    for (let t = 0; t < 2; t++) {
      await act(async () => { vi.advanceTimersByTime(part) })
      await settle()
    }
    // Under one shared budget the timer would have fired during the second pattern
    // and blamed it; each pattern's own budget keeps both.
    expect(workers.made).toHaveLength(1)
    expect(workers.made[0].terminated).toBe(false)
    expect(spans(container)).toEqual(['S-1', 'TK-1234'])
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('never sends a text node longer than the cap to the worker', async () => {
    const workers = fakeWorkers()
    setTextPatternWorkerFactoryForTest(workers.factory)
    use([TICKET])
    const long = `TK-1234 ${'x'.repeat(MAX_SCAN_SUBJECT)}`
    expect(textPatternHits(long)).toEqual([])
    const { container } = render(<MarkdownRenderer textPreviews content={long} />)
    await settle()
    expect(spans(container)).toEqual([])
    expect(workers.made.flatMap((w) => w.scans).some((s) => s.texts.some((t) => t.length > MAX_SCAN_SUBJECT))).toBe(false)
  })

  it('a full cache keeps its answers and leaves new text plain, instead of starting over', async () => {
    const workers = fakeWorkers()
    setTextPatternWorkerFactoryForTest(workers.factory)
    use([TICKET])
    const texts = Array.from({ length: MAX_CACHED_TEXTS }, (_, i) => `TK-${1000 + i}`)
    for (const t of texts) textPatternHits(t)
    await settle()
    expect(textPatternHits(texts[0]).length).toBe(1)
    const scans = workers.made.flatMap((w) => w.scans).length
    expect(textPatternHits('TK-99999999')).toEqual([])
    await settle()
    // Nothing new was sent, and the old answers are still there.
    expect(workers.made.flatMap((w) => w.scans).length).toBe(scans)
    expect(textPatternHits(texts[MAX_CACHED_TEXTS - 1]).length).toBe(1)
  })

  it('never runs a pattern on the calling thread: hits come only from the worker', () => {
    use([TICKET])
    expect(textPatternHits('TK-1234')).toEqual([])
    expect(resolveTextPreview('TK-1234')).toBeNull()
  })

  it('without a Worker, no spans are drawn rather than matching on the page', async () => {
    setTextPatternWorkerFactoryForTest(() => null)
    use([TICKET])
    const { container } = render(<MarkdownRenderer textPreviews content="TK-1234" />)
    await settle()
    expect(spans(container)).toEqual([])
  })
})

describe('textPatterns: declaring', () => {
  it('drops a pattern that is overlong, does not compile or has bad flags, without running any', async () => {
    use([{ pattern: 'a'.repeat(201) }, { pattern: '(' }, { pattern: '\\bQQ\\b', flags: 'gy' }])
    expect(hasTextPatterns()).toBe(false)
  })

  it('declaring a pattern runs none of it on this thread, even one that is catastrophic on the empty string', () => {
    const EMPTY = '(?:a?|b?){35}(?!$)'
    const ran: string[] = []
    const realTest = RegExp.prototype.test
    const realExec = RegExp.prototype.exec
    const test = vi.spyOn(RegExp.prototype, 'test').mockImplementation(function (this: RegExp, s: string) { ran.push(this.source); return realTest.call(this, s) })
    const exec = vi.spyOn(RegExp.prototype, 'exec').mockImplementation(function (this: RegExp, s: string) { ran.push(this.source); return realExec.call(this, s) })
    use([{ id: 'empty', pattern: EMPTY }, TICKET])
    test.mockRestore()
    exec.mockRestore()
    expect(ran.some((src) => src.includes(EMPTY) || src.includes('TK-'))).toBe(false)
    expect(hasTextPatterns()).toBe(true)
  })

  it('a pattern that matches the empty string is found by the worker and turned off; the rest still match', async () => {
    const workers = fakeWorkers()
    setTextPatternWorkerFactoryForTest(workers.factory)
    use([{ id: 'empty', pattern: 'x*' }, TICKET])
    const { container } = render(<MarkdownRenderer textPreviews content="see TK-1234" />)
    await settle()
    expect(spans(container)).toEqual(['TK-1234'])
    render(<MarkdownRenderer textPreviews content="and TK-5678" />)
    await settle()
    // Once rejected it is never sent again.
    expect(workers.made.flatMap((w) => w.scans).slice(1).every((s) => s.patterns.every((p) => p.source === TICKET.pattern))).toBe(true)
  })

  it('a worker that never starts turns plain-text previews off for the page, once', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const made: number[] = []
    setTextPatternWorkerFactoryForTest(() => {
      made.push(1)
      return { onmessage: null, terminate() {}, postMessage() {} }
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    use([TICKET])
    const { container } = render(<MarkdownRenderer textPreviews content="TK-1234" />)
    await settle()
    // The budget has not started: a cold worker is not a pattern's fault.
    await act(async () => { vi.advanceTimersByTime(SCAN_BUDGET_MS + 1) })
    expect(warn).not.toHaveBeenCalled()
    await act(async () => { vi.advanceTimersByTime(WORKER_START_TIMEOUT_MS) })
    expect(warn).toHaveBeenCalledTimes(1)
    render(<MarkdownRenderer textPreviews content="TK-5678" />)
    await settle()
    expect(made).toHaveLength(1)
    expect(spans(container)).toEqual([])
    warn.mockRestore()
  })

  it('caps the list at 16 patterns', async () => {
    const many = Array.from({ length: 17 }, (_, i) => ({ pattern: `\\bQ${i}X-\\d+\\b` }))
    use(many)
    const { container } = render(<MarkdownRenderer textPreviews content="Q15X-1 Q16X-1" />)
    await settle()
    expect(spans(container)).toEqual(['Q15X-1'])
  })

  it("keeps an app's own shapes and flags as declared", async () => {
    use([
      { id: 'ticket', pattern: '(?<![\\w-])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\\d{2,})(?![\\w-])', flags: 'g' },
      { id: 'team', pattern: '(?<![\\w.-])(team\\.[a-z0-9]+)(?![\\w.-])', flags: 'gi' },
    ])
    const { container } = render(<MarkdownRenderer textPreviews content="ABC-DEF-12 and TEAM.ZQ1" />)
    await settle()
    expect(spans(container)).toEqual(['ABC-DEF-12', 'TEAM.ZQ1'])
    expect(resolveTextPreview('ABC-DEF-12')).toEqual(OWNER)
  })

  it('honours defaultOff until the app reports, then what it reports, across reloads', async () => {
    use([{ id: 'acronym', pattern: '(?<![\\w])([A-Z]{2,8})(?![\\w])', defaultOff: true }, { id: 'ticket', ...TICKET }])
    const { container } = render(<MarkdownRenderer textPreviews content="ZQT and TK-1234" />)
    await settle()
    expect(spans(container)).toEqual(['TK-1234'])
    act(() => setTextPatternsOff('cards-app', ['ticket']))
    await settle()
    // The acronym pattern is on now, and it also matches the TK prefix of TK-1234.
    expect(spans(container)).toEqual(['ZQT', 'TK'])
    expect(window.localStorage.getItem('kirocrew:link-preview-text-off:cards-app')).toBe('["ticket"]')
  })

  it('bumps the generation only on a change', () => {
    use([TICKET])
    const g = textPatternGeneration()
    use([TICKET])
    expect(textPatternGeneration()).toBe(g)
    use([TICKET, PERSON])
    expect(textPatternGeneration()).toBeGreaterThan(g)
  })
})

describe('textPatterns: rendering', () => {
  it('wraps matches in chat prose in spans, never links', async () => {
    use([TICKET, PERSON])
    const { container } = render(<MarkdownRenderer textPreviews content="ask alice@ about TK-1234, and TK-5678." />)
    await settle()
    expect(spans(container)).toEqual(['alice@', 'TK-1234', 'TK-5678'])
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.querySelector('.link-preview-text')).not.toBeNull()
  })

  it('leaves code, inline code, links and autolinked tokens alone', async () => {
    use([TICKET])
    registerAutolinkRules([{ id: 'edition-tk', pattern: /\bTK-9\d{3}\b/g, href: 'https://tickets.example.com/t/{match}' }])
    const md = [
      '`TK-1111` and [TK-2222](https://x.example.com/) and https://x.example.com/TK-3333',
      '',
      '```',
      'TK-4444',
      '```',
      '',
      'TK-9999 then TK-5555',
    ].join('\n')
    const { container } = render(<MarkdownRenderer textPreviews content={md} />)
    await settle()
    expect(spans(container)).toEqual(['TK-5555'])
    expect(container.querySelector('a[href="https://tickets.example.com/t/TK-9999"]')).not.toBeNull()
  })

  it('draws nothing without the textPreviews prop', async () => {
    use([TICKET])
    const { container } = render(<MarkdownRenderer content="TK-1234" />)
    await settle()
    expect(spans(container)).toEqual([])
  })

  it('re-renders an already painted message when patterns arrive', async () => {
    const { container } = render(<MarkdownRenderer textPreviews content="TK-1234" />)
    expect(spans(container)).toEqual([])
    act(() => use([TICKET]))
    await settle()
    expect(spans(container)).toEqual(['TK-1234'])
  })
})

describe('textPatterns: hover', () => {
  const mint = vi.fn(async (appName: string) => ({ frame: `/app-frame/${appName}/lp/f/tok`, capability: 'cap', ttl: 3600 }))
  async function mountLayer(apps: unknown[]) {
    listApps.mockResolvedValue(apps)
    const mod = await import('./LinkPreviewLayer')
    mod.setLinkPreviewMintForTest(mint)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(createElement(QueryClientProvider, { client: qc }, createElement(mod.default)))
  }
  /** Hover `el`, let its frame load and answer ready; the frame and what it was sent. */
  async function hover(el: HTMLElement) {
    fireEvent.pointerOver(el)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const frames = framesIn()
    for (const f of frames) fromFrame(f, 'ready')
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    return frames
  }
  afterEach(async () => {
    (await import('./LinkPreviewLayer')).setLinkPreviewMintForTest()
    mint.mockClear()
  })

  it('a plain-text span sends its provider the matched text and the span rect, never the span', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await mountLayer([app([PERSON])])
    // The app list query resolves, then the layer registers its patterns in an effect.
    await waitFor(() => expect(hasTextPatterns()).toBe(true), { timeout: 5000 })
    const { container } = render(<MarkdownRenderer textPreviews content="ping alice@ now" />)
    await settle()
    const span = container.querySelector('[data-link-preview-text]') as HTMLElement
    const [frame] = await hover(span)
    const show = sentTo(frame).find((m) => m.type === 'show')
    expect(show).toMatchObject({ text: 'alice@', rect: { left: expect.any(Number), top: expect.any(Number) } })
    expect(show && 'url' in show).toBe(false)
    expect(Object.values(show ?? {}).some((v) => v instanceof Node)).toBe(false)
  })

  it('the span goes to the app whose pattern matched, even when another app claims the same host first', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await mountLayer([
      { name: 'first-app', enabled: true, manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/a.mjs', hosts: ['tickets.example.com'] }] } } },
      { name: 'second-app', enabled: true, manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/b.mjs', hosts: ['tickets.example.com'], textPatterns: [TICKET] }] } } },
    ])
    // The app list query resolves, then the layer registers its patterns in an effect.
    await waitFor(() => expect(hasTextPatterns()).toBe(true), { timeout: 5000 })
    const { container } = render(<MarkdownRenderer textPreviews content="see TK-1234" />)
    await settle()
    const frames = await hover(container.querySelector('[data-link-preview-text]') as HTMLElement)
    expect(mint.mock.calls.map((c) => c[0])).toEqual(['second-app'])
    expect(frames.map((f) => f.dataset.linkPreviewFrame)).toEqual(['second-app:cards'])
  })

  it('a span is owned by the first app whose pattern takes its whole text, in every message', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await mountLayer([
      { name: 'b-app', enabled: true, manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/b.mjs', hosts: ['b.example.com'], textPatterns: [{ pattern: '^TK-\\d+$' }] }] } } },
      { name: 'c-app', enabled: true, manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/c.mjs', hosts: ['c.example.com'], textPatterns: [{ pattern: 'TK-\\d+' }] }] } } },
    ])
    await waitFor(() => expect(hasTextPatterns()).toBe(true), { timeout: 5000 })
    // In prose only the second app's pattern finds it, but the first takes its whole
    // text, so the first owns it from the start; no other message is needed.
    render(<MarkdownRenderer textPreviews content="see TK-1234" />)
    await settle()
    expect(resolveTextPreview('TK-1234')).toEqual({ owner: 'b-app:cards' })
    render(<MarkdownRenderer textPreviews content="TK-1234" />)
    await settle()
    expect(resolveTextPreview('TK-1234')).toEqual({ owner: 'b-app:cards' })
  })

  it('a forged span whose text no pattern matched opens nothing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await mountLayer([app([PERSON])])
    // The app list query resolves, then the layer registers its patterns in an effect.
    await waitFor(() => expect(hasTextPatterns()).toBe(true), { timeout: 5000 })
    const forged = document.createElement('span')
    forged.setAttribute('data-link-preview-text', '')
    forged.textContent = 'alice@'
    document.body.append(forged)
    expect(await hover(forged)).toEqual([])
    expect(mint).not.toHaveBeenCalled()
  })
})
