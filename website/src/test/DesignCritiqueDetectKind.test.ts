// What `detectKind` decides a pasted reference is. A repo is cloned and its
// screens listed; a url is captured as a live page. A `www.` repo link used to
// fall through to a live-page capture, and a loopback host typed without a scheme
// was only recognised for two of the four hosts the schemed form accepts.
import { describe, expect, it } from 'vitest'

import { detectKind } from '../apps/design-critique/utils'

describe('detectKind', () => {
  it.each([
    ['https://www.github.com/o/r', 'https://github.com/o/r'],
    ['http://www.gitlab.com/o/r', 'http://gitlab.com/o/r'],
    ['www.github.com/o/r', 'https://github.com/o/r'],
    ['https://bitbucket.org/o/r', 'https://bitbucket.org/o/r'],
    ['bitbucket.org/o/r', 'https://bitbucket.org/o/r'],
    ['https://WWW.github.com/o/r/tree/main', 'https://github.com/o/r/tree/main'],
  ])('reads %s as a repo', (raw, value) => {
    expect(detectKind(raw)).toEqual({ kind: 'repo', value })
  })

  it.each([
    ['localhost', 'http://localhost'],
    ['localhost/app', 'http://localhost/app'],
    // Discovery refuses the unspecified address, so the value names 127.0.0.1.
    ['0.0.0.0:5173', 'http://127.0.0.1:5173'],
    ['0.0.0.0', 'http://127.0.0.1'],
    ['[::1]:5173', 'http://[::1]:5173'],
    ['127.0.0.1', 'http://127.0.0.1'],
  ])('reads bare %s as a local url', (raw, value) => {
    expect(detectKind(raw)).toEqual({ kind: 'url', value, local: true })
  })

  it('sends a schemed 0.0.0.0 link to 127.0.0.1, the address discovery accepts', () => {
    expect(detectKind('http://0.0.0.0:5173/app')).toEqual({ kind: 'url', value: 'http://127.0.0.1:5173/app', local: true })
  })

  it('keeps a word that only starts with a loopback name unknown', () => {
    expect(detectKind('localhostfoo')?.kind).toBe('unknown')
  })

  // Inputs that already worked keep the same result.
  it.each([
    ['https://github.com/o/r', { kind: 'repo', value: 'https://github.com/o/r' }],
    ['github.com/o/r', { kind: 'repo', value: 'https://github.com/o/r' }],
    ['http://gitlab.com/o/r', { kind: 'repo', value: 'http://gitlab.com/o/r' }],
    ['localhost:5173', { kind: 'url', value: 'http://localhost:5173', local: true }],
    ['127.0.0.1:3000/x', { kind: 'url', value: 'http://127.0.0.1:3000/x', local: true }],
    ['http://[::1]:5173', { kind: 'url', value: 'http://[::1]:5173', local: true }],
    ['https://example.com', { kind: 'url', value: 'https://example.com' }],
    ['https://www.figma.com/file/x', { kind: 'figma', value: 'https://www.figma.com/file/x' }],
    ['/Users/me/app', { kind: 'local', value: '/Users/me/app' }],
    ['hello', { kind: 'unknown', value: 'hello' }],
  ])('keeps %s as before', (raw, want) => {
    expect(detectKind(raw)).toEqual(want)
  })
})
