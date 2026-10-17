import { afterEach, describe, expect, it } from 'vitest'
import { _resetSafeReloadForTests, captureSafeReload, isSafeReload, reloadKeepingSafe } from './safeReload'

function fakeWindow(href: string) {
  const calls: string[] = []
  const win = {
    location: { href } as Location,
    history: { state: { k: 1 }, replaceState: (_s: unknown, _t: string, url: string) => { calls.push(url) } } as unknown as History,
  }
  return { win, calls }
}

afterEach(() => _resetSafeReloadForTests())

describe('captureSafeReload', () => {
  it('reads safe=1 and strips only that param from the address', () => {
    const { win, calls } = fakeWindow('http://localhost:5476/?token=abc&safe=1#x')
    expect(captureSafeReload(win)).toBe(true)
    expect(isSafeReload()).toBe(true)
    expect(calls).toEqual(['/?token=abc#x'])
  })

  it('is off for a normal load and leaves the address alone', () => {
    const { win, calls } = fakeWindow('http://localhost:5476/chat?sid=chat-1')
    expect(captureSafeReload(win)).toBe(false)
    expect(isSafeReload()).toBe(false)
    expect(calls).toEqual([])
  })

  it('ignores a value other than 1 but still strips it', () => {
    const { win, calls } = fakeWindow('http://localhost:5476/?safe=0')
    expect(captureSafeReload(win)).toBe(false)
    expect(calls).toEqual(['/'])
  })
})

describe('reloadKeepingSafe', () => {
  function reloadWindow(href: string) {
    const log: string[] = []
    const win = {
      location: {
        href,
        reload: () => { log.push('reload') },
        replace: (url: string) => { log.push(`replace ${url}`) },
      } as unknown as Location,
    }
    return { win, log }
  }

  it('puts safe=1 back when a safe load reloads itself during boot', () => {
    captureSafeReload(fakeWindow('http://localhost:5476/?token=abc&safe=1').win)
    const { win, log } = reloadWindow('http://localhost:5476/?token=abc')
    reloadKeepingSafe(win)
    expect(log).toEqual(['replace http://localhost:5476/?token=abc&safe=1'])
  })

  it('is a plain reload on a normal load', () => {
    captureSafeReload(fakeWindow('http://localhost:5476/?token=abc').win)
    const { win, log } = reloadWindow('http://localhost:5476/?token=abc')
    reloadKeepingSafe(win)
    expect(log).toEqual(['reload'])
  })
})
