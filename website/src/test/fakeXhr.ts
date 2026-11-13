/**
 * A scripted XMLHttpRequest for the composer upload, the one request the
 * client sends over XHR (`fetch` reports no upload progress). Each `send`
 * lands in `FakeXhr.sent` and the test answers it: `respond`, `fail` (the
 * network drops), `start` (the body starts going out) or `progress` (a step
 * of it). `abort` is a spy.
 */
import { expect, vi } from 'vitest'

export class FakeXhr {
  static sent: FakeXhr[] = []
  method = ''
  url = ''
  body: unknown = undefined
  requestHeaders: Record<string, string> = {}
  status = 0
  statusText = ''
  responseText = ''
  readonly upload: Record<'onloadstart' | 'onprogress', ((e: { loaded: number; total: number }) => void) | null> = {
    onloadstart: null, onprogress: null,
  }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  private responseHeaders = ''
  readonly abort = vi.fn()

  open(method: string, url: string) { this.method = method; this.url = url }
  setRequestHeader(name: string, value: string) { this.requestHeaders[name] = value }
  getAllResponseHeaders() { return this.responseHeaders }
  send(body: unknown) { this.body = body; FakeXhr.sent.push(this) }

  start(total: number) { this.upload.onloadstart?.({ loaded: 0, total }) }
  progress(loaded: number, total: number) { this.upload.onprogress?.({ loaded, total }) }
  respond(status: number, body: unknown, opts: { statusText?: string; headers?: Record<string, string> } = {}) {
    this.status = status
    this.statusText = opts.statusText ?? ''
    this.responseText = typeof body === 'string' ? body : JSON.stringify(body)
    this.responseHeaders = Object.entries(opts.headers ?? {}).map(([k, v]) => `${k}: ${v}\r\n`).join('')
    this.onload?.()
  }
  fail() { this.onerror?.() }
}

/** Install `FakeXhr` as the global XMLHttpRequest; `vi.unstubAllGlobals` removes it. */
export function stubXhr() {
  FakeXhr.sent = []
  vi.stubGlobal('XMLHttpRequest', FakeXhr)
}

/** The next request sent, once the upload's async preparation reaches the wire. */
export async function nextXhr(): Promise<FakeXhr> {
  await vi.waitFor(() => expect(FakeXhr.sent.length).toBeGreaterThan(0))
  return FakeXhr.sent.shift()!
}
