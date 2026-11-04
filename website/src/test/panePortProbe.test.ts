/**
 * The browser-side pane port probe (utils/panePortProbe.ts) and the fact it adds
 * to the remote-crew failure report (utils/instanceFailureReport.ts).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { canProbePanePort, panePortUrl, probePanePort } from '../utils/panePortProbe'
import { reportInstanceFailure, __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'
import { __resetErrorJournalForTests } from '../utils/errorReport'

beforeEach(() => {
  __resetInstanceFailuresForTests()
  __resetErrorJournalForTests()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('canProbePanePort', () => {
  it('admits only the IPv4 loopback hosts the dashboard CSP lets it connect to', () => {
    expect(canProbePanePort('localhost')).toBe(true)
    expect(canProbePanePort('127.0.0.1')).toBe(true)
    expect(canProbePanePort('0.0.0.0')).toBe(true)
    // Framable but not in connect-src: a probe would be CSP-refused and misread.
    expect(canProbePanePort('crew.localhost')).toBe(false)
    expect(canProbePanePort('example.com')).toBe(false)
  })
})

describe('probePanePort', () => {
  it('is reachable when anything answers and sends a no-cors, credential-free GET', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))
    await expect(probePanePort(panePortUrl('localhost', 7778))).resolves.toBe('reachable')
    expect(fetchSpy).toHaveBeenCalledWith(
      'http://localhost:7778/',
      expect.objectContaining({ mode: 'no-cors', cache: 'no-store', credentials: 'omit' }),
    )
  })

  it('is unreachable when the connection is refused', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(probePanePort('http://localhost:7778/')).resolves.toBe('unreachable')
  })

  it('is unknown, not unreachable, when nothing answers before the timeout', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))
    const result = probePanePort('http://localhost:7778/', 3_000)
    await vi.advanceTimersByTimeAsync(3_000)
    await expect(result).resolves.toBe('unknown')
  })
})

describe('reportInstanceFailure with an unreachable pane port', () => {
  const base = {
    id: 'cd-1',
    name: 'Cloud One',
    transport: 'ssh',
    status: { instance_id: 'cd-1', state: 'connected' as const, local_port: 7778, remote_port: 7777 },
    stage: 'pane_load' as const,
    fallbackMessage: 'Pane failed to load',
  }

  it('adds the probed URL and its verdict as report lines', () => {
    const report = reportInstanceFailure({ ...base, unreachablePanePort: { host: 'localhost', port: 7778 } })
    expect(report?.detail).toContain('pane port: http://localhost:7778/')
    expect(report?.detail).toContain('pane port probe: unreachable')
  })

  it('adds no probe line when the port was not found unreachable', () => {
    const report = reportInstanceFailure(base)
    expect(report?.detail).not.toContain('pane port')
  })
})
