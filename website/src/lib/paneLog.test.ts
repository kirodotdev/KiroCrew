import { afterEach, describe, expect, it, vi } from 'vitest'

import { PANE_LOG_PREFIX, frameDocumentState, paneLog, safePaneUrl } from './paneLog'

function captureLines(run: () => void): string[] {
  const spy = vi.spyOn(console, 'info').mockImplementation(() => {})
  try {
    run()
    return spy.mock.calls.map(call => String(call[0]))
  } finally {
    spy.mockRestore()
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('paneLog', () => {
  it('emits one greppable line with the shared prefix', () => {
    const [line] = captureLines(() => paneLog('ready', { id: 'nobita', port: 7778 }))
    expect(line).toBe(`${PANE_LOG_PREFIX} ready id=nobita port=7778`)
  })

  it('never journals a credential, whatever the caller passes', () => {
    const [line] = captureLines(() =>
      paneLog('warm', { id: 'nobita', token: 'supersecret', secret: 'x', password: 'y' }),
    )
    expect(line).not.toContain('supersecret')
    expect(line).toContain('token=<redacted>')
    expect(line).toContain('secret=<redacted>')
    expect(line).toContain('password=<redacted>')
    // The key test is a substring match on purpose, so a wrapped name is caught too.
    expect(captureLines(() => paneLog('warm', { authToken: 'zzz' }))[0]).toBe(
      `${PANE_LOG_PREFIX} warm authToken=<redacted>`,
    )
  })

  it('keeps a boolean presence flag, which is the finding and not the secret', () => {
    const [line] = captureLines(() => paneLog('remint-empty', { id: 'nobita', hasToken: false }))
    expect(line).toBe(`${PANE_LOG_PREFIX} remint-empty id=nobita hasToken=false`)
    expect(line).not.toContain('<redacted>')
    // Only booleans are exempt: a string under the same key is still a credential.
    expect(captureLines(() => paneLog('e', { hasToken: 'abc' }))[0]).toContain('hasToken=<redacted>')
  })

  it('drops undefined fields instead of printing the word "undefined"', () => {
    const [line] = captureLines(() => paneLog('warm-declined', { id: 'a', error: undefined }))
    expect(line).toBe(`${PANE_LOG_PREFIX} warm-declined id=a`)
    expect(line).not.toContain('undefined')
  })

  it('quotes a value containing spaces so the line stays parseable', () => {
    const [line] = captureLines(() => paneLog('warm-failed', { error: 'connection refused' }))
    expect(line).toBe(`${PANE_LOG_PREFIX} warm-failed error="connection refused"`)
  })

  it('keeps booleans and null readable', () => {
    const [line] = captureLines(() => paneLog('e', { a: true, b: false, c: null }))
    expect(line).toBe(`${PANE_LOG_PREFIX} e a=true b=false c=null`)
  })

  it('works with no fields at all', () => {
    const [line] = captureLines(() => paneLog('retry'))
    expect(line).toBe(`${PANE_LOG_PREFIX} retry`)
  })
})

describe('safePaneUrl', () => {
  it('drops the token value but records that there was one', () => {
    expect(safePaneUrl('http://localhost:7778/?token=abc123')).toBe(
      'http://localhost:7778/?token=<redacted>',
    )
  })

  it('keeps the port, which is the diagnostic content', () => {
    expect(safePaneUrl('http://localhost:7778/?token=abc123')).toContain(':7778')
  })

  it('marks a non-token query without echoing it', () => {
    expect(safePaneUrl('http://localhost:5476/?foo=1')).toBe('http://localhost:5476/?<query>')
  })

  it('names an empty src explicitly — that is the failure mode, not a gap', () => {
    expect(safePaneUrl('')).toBe('<empty>')
    expect(safePaneUrl(null)).toBe('<empty>')
    expect(safePaneUrl(undefined)).toBe('<empty>')
  })

  it('passes a query-less URL through untouched', () => {
    expect(safePaneUrl('http://localhost:5476/')).toBe('http://localhost:5476/')
  })
})

describe('frameDocumentState', () => {
  it('reports cross-origin when reading location throws — the pane DID navigate', () => {
    const el = {
      contentWindow: {
        get location(): never {
          throw new DOMException('blocked a frame with origin')
        },
      },
    } as unknown as HTMLIFrameElement
    expect(frameDocumentState(el)).toBe('cross-origin')
  })

  it('reports the readable about:blank — the pane never navigated', () => {
    const el = {
      contentWindow: { location: { href: 'about:blank' } },
    } as unknown as HTMLIFrameElement
    expect(frameDocumentState(el)).toBe('about:blank')
  })

  it('redacts a token even when the frame is readable', () => {
    const el = {
      contentWindow: { location: { href: 'http://localhost:5476/?token=abc' } },
    } as unknown as HTMLIFrameElement
    expect(frameDocumentState(el)).toBe('http://localhost:5476/?token=<redacted>')
  })

  it('distinguishes a missing element from a missing contentWindow', () => {
    expect(frameDocumentState(null)).toBe('no-element')
    expect(frameDocumentState(undefined)).toBe('no-element')
    expect(frameDocumentState({ contentWindow: null } as unknown as HTMLIFrameElement)).toBe(
      'no-contentwindow',
    )
  })
})
