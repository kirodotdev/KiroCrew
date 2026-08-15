import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

/**
 * Monaco is pointed at the locally-bundled package (the CDN is blocked on
 * corporate networks) and its web-worker factory at Vite-bundled entries. Both
 * halves are asserted here: `loader.config` receives the local instance exactly
 * once however many callers race, and every label Monaco asks about maps to a
 * worker rather than silently degrading to the main thread.
 */
const mocks = vi.hoisted(() => ({
  config: vi.fn(),
  monaco: { editor: { zzz: true } },
}))

vi.mock('@monaco-editor/react', () => ({ loader: { config: mocks.config } }))
vi.mock('monaco-editor', () => mocks.monaco)

class FakeWorker {
  constructor(public url: URL | string, public opts?: { type?: string }) {}
}

interface MonacoEnv { getWorker(id: string, label: string): FakeWorker }

let ensureMonacoLocal: () => Promise<void>

beforeEach(async () => {
  mocks.config.mockClear()
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  ;(self as unknown as { MonacoEnvironment?: unknown }).MonacoEnvironment = undefined
  ensureMonacoLocal = (await import('../utils/monacoLocal')).ensureMonacoLocal
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function env(): MonacoEnv {
  return (self as unknown as { MonacoEnvironment: MonacoEnv }).MonacoEnvironment
}

describe('ensureMonacoLocal', () => {
  it('hands the locally-bundled monaco instance to the loader', async () => {
    await ensureMonacoLocal()
    expect(mocks.config).toHaveBeenCalledTimes(1)
    expect(mocks.config).toHaveBeenCalledWith({ monaco: mocks.monaco })
  })

  it('is idempotent — concurrent callers share one configuration', async () => {
    const [a, b] = [ensureMonacoLocal(), ensureMonacoLocal()]
    await Promise.all([a, b])
    await ensureMonacoLocal()
    expect(mocks.config).toHaveBeenCalledTimes(1)
  })

  it('installs a worker factory covering every label Monaco asks for', async () => {
    await ensureMonacoLocal()
    const factory = env()
    expect(typeof factory.getWorker).toBe('function')

    const urlFor = (label: string) => String(factory.getWorker('1', label).url)
    expect(urlFor('json')).toContain('json.worker')
    expect(urlFor('css')).toContain('css.worker')
    expect(urlFor('scss')).toContain('css.worker')
    expect(urlFor('less')).toContain('css.worker')
    expect(urlFor('html')).toContain('html.worker')
    expect(urlFor('handlebars')).toContain('html.worker')
    expect(urlFor('razor')).toContain('html.worker')
    expect(urlFor('typescript')).toContain('ts.worker')
    expect(urlFor('javascript')).toContain('ts.worker')
    // Anything else gets the generic editor worker rather than nothing.
    expect(urlFor('zzz-unknown')).toContain('editor.worker')
  })

  it('creates module workers (Vite ships ESM worker entries)', async () => {
    await ensureMonacoLocal()
    const worker = env().getWorker('1', 'json')
    expect(worker).toBeInstanceOf(FakeWorker)
    expect(worker.opts).toEqual({ type: 'module' })
  })
})
