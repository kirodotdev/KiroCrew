import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { captureUrlToken, hasUrlToken, takeUrlToken } from './urlToken'

function fakeWindow(href: string) {
  const calls: string[] = []
  const win = {
    location: { href } as Location,
    history: { state: { k: 1 }, replaceState: (_s: unknown, _t: string, url: string) => { calls.push(url) } } as unknown as History,
  }
  return { win, calls }
}

afterEach(() => { takeUrlToken() })

describe('captureUrlToken', () => {
  it('strips only the token from the address before anything renders, and keeps it for one read', () => {
    const { win, calls } = fakeWindow('http://localhost:5476/chat?sid=chat-1&token=abc.def#x')
    captureUrlToken(win)
    expect(calls).toEqual(['/chat?sid=chat-1#x'])
    expect(hasUrlToken()).toBe(true)
    expect(takeUrlToken()).toBe('abc.def')
    expect(hasUrlToken()).toBe(false)
    expect(takeUrlToken()).toBeNull()
  })

  it('leaves an address without a token alone', () => {
    const { win, calls } = fakeWindow('http://localhost:5476/chat?sid=chat-1')
    captureUrlToken(win)
    expect(calls).toEqual([])
    expect(hasUrlToken()).toBe(false)
  })

  it('strips an empty token without keeping one', () => {
    const { win, calls } = fakeWindow('http://localhost:5476/?token=')
    captureUrlToken(win)
    expect(calls).toEqual(['/'])
    expect(hasUrlToken()).toBe(false)
  })

  it('runs in boot before the first render, so no screen ever shows the token', () => {
    const main = readFileSync(resolve(__dirname, '../main.tsx'), 'utf8')
    const boot = main.slice(main.indexOf('function boot('))
    expect(boot.indexOf('captureUrlToken()')).toBeGreaterThan(-1)
    expect(boot.indexOf('captureUrlToken()')).toBeLessThan(boot.indexOf('createRoot('))
  })
})
