import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => true) }))
import { isEmbeddedPane } from '../lib/embedded'
import { noteHostOrigin, postToHost } from '../lib/hostRelay'

// #21822: the pane -> hub relay posts only to the origin the hub's model came
// from, and sends nothing when that origin is unknown.
describe('postToHost', () => {
  let post: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
  })
  afterEach(() => {
    noteHostOrigin(null)
    post.mockRestore()
  })

  it.each([
    ['http 127.0.0.1', 'http://127.0.0.1:7777'],
    ['http localhost', 'http://localhost:5476'],
    ['http *.localhost', 'http://crew.localhost:5476'],
    ['https loopback', 'https://127.0.0.1:7777'],
    ['https localhost, default port', 'https://localhost'],
    ['0.0.0.0', 'http://0.0.0.0:7777'],
  ])('posts to the exact origin of a %s hub', (_label, origin) => {
    noteHostOrigin(origin)
    expect(postToHost({ type: 'mc-switch-instance', v: 1, id: null })).toBe(true)
    expect(post).toHaveBeenCalledWith({ type: 'mc-switch-instance', v: 1, id: null }, origin)
  })

  it('sends nothing before any model has arrived', () => {
    expect(postToHost({ type: 'mc-switch-instance', v: 1, id: null })).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it.each([
    ['null (opaque sender)', 'null'],
    ['empty', ''],
    ['non-loopback https', 'https://evil.example'],
    ['non-loopback http', 'http://10.0.0.5:7777'],
    ['https *.localhost (not embeddable)', 'https://crew.localhost:7777'],
    ['IPv6 loopback (not embeddable)', 'http://[::1]:7777'],
    ['an origin with a path', 'http://127.0.0.1:7777/chat'],
  ])('sends nothing when the model came from %s', (_label, origin) => {
    noteHostOrigin(origin)
    expect(postToHost({ type: 'mc-set-stable-order', v: 1, on: true })).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('a later model from a non-hub origin clears an earlier good one', () => {
    noteHostOrigin('http://127.0.0.1:7777')
    noteHostOrigin('https://evil.example')
    expect(postToHost({ type: 'mc-set-crew-pin', v: 1, id: 'a' })).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('sends nothing when not embedded', () => {
    noteHostOrigin('http://127.0.0.1:7777')
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    expect(postToHost({ type: 'mc-switch-instance', v: 1, id: null })).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('never throws when postMessage throws', () => {
    noteHostOrigin('http://127.0.0.1:7777')
    post.mockImplementation(() => { throw new Error('torn down') })
    expect(postToHost({ type: 'mc-switch-instance', v: 1, id: null })).toBe(false)
  })
})
