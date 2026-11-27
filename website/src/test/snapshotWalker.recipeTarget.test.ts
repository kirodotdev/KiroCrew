/**
 * The walker's `recipeTarget` and `recipeClick` methods
 * (`src/kiro_crew/browser_harness/snapshot_walker.js`), which find and click an app
 * browser recipe's element (`browser_harness/recipes.py`): the first match, only when
 * shown, never a password box, never under something drawn over it, and the click
 * goes to that element itself (`el.click()`), never to a point.
 */
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setHtml } from './domFixture'
import { readSource } from './readSource'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readSource(join(HARNESS_DIR, 'snapshot_walker.js')).split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

interface Walker {
  recipeTarget: (selector: string) => { ok: boolean; code?: string; x?: number; y?: number }
  recipeClick: (selector: string, site?: string, sites?: string[]) => { ok: boolean; code?: string; x?: number; y?: number }
}

const SITE = 'https://www.youtube.com/watch'

const walker = (): Walker => new Function(`return (${walkerSource()})`)() as Walker

const realElementRect = Element.prototype.getBoundingClientRect
const realComputedStyle = window.getComputedStyle.bind(window)
/** What `elementFromPoint` answers: by default the element under test itself. */
let drawnOnTop: (() => Element | null) | null = null

beforeEach(() => {
  vi.stubGlobal('location', new URL('https://www.youtube.com/watch?v=abc'))
  Element.prototype.getBoundingClientRect = function () {
    return { x: 10, y: 10, left: 10, top: 10, width: 100, height: 20, right: 110, bottom: 30, toJSON: () => ({}) } as DOMRect
  }
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el) => new Proxy(realComputedStyle(el), {
    get(style, key) {
      const value = Reflect.get(style, key)
      if (key === 'visibility') return value || 'visible'
      if (key === 'display' && (el as HTMLElement).id === 'hidden') return 'none'
      return value
    },
  }))
  drawnOnTop = null
  ;(document as unknown as { elementFromPoint: (x: number, y: number) => Element | null }).elementFromPoint = () =>
    drawnOnTop ? drawnOnTop() : document.querySelector('.skip span') || document.querySelector('.skip')
})

afterEach(() => {
  vi.unstubAllGlobals()
  Element.prototype.getBoundingClientRect = realElementRect
  vi.restoreAllMocks()
})

describe('recipeTarget', () => {
  it('answers the centre of a shown element, a click on its own child included', () => {
    setHtml(document.body, '<button class="skip"><span>Skip</span></button>')
    expect(walker().recipeTarget('.skip')).toEqual({ ok: true, x: 60, y: 20 })
  })

  it('refuses an element something else is drawn over', () => {
    setHtml(document.body, '<button class="skip">Skip</button><div class="modal">Sign in</div>')
    drawnOnTop = () => document.querySelector('.modal')
    expect(walker().recipeTarget('.skip')).toEqual({ ok: false, code: 'covered' })
  })

  it('names a missing, hidden or password element', () => {
    setHtml(document.body, '<button id="hidden" class="skip">Skip</button><input type="password" class="pw">')
    expect(walker().recipeTarget('.none').code).toBe('not_found')
    expect(walker().recipeTarget('.skip').code).toBe('hidden')
    expect(walker().recipeTarget('.pw').code).toBe('password_field')
  })

  it('refuses a selector the page cannot parse', () => {
    setHtml(document.body, '<button class="skip">Skip</button>')
    expect(walker().recipeTarget('[[').code).toBe('bad_selector')
  })
})

describe('recipeClick', () => {
  it.each(['iframe', 'frame', 'object', 'embed'])('refuses a %s hit inside a container or as the target', (tag) => {
    setHtml(document.body, '<div class="skip"></div>')
    const embedded = document.createElement(tag)
    document.querySelector('.skip')!.append(embedded)
    drawnOnTop = () => embedded
    for (const selector of ['.skip', tag]) {
      expect(walker().recipeTarget(selector)).toEqual({ ok: false, code: 'covered' })
      expect(walker().recipeClick(selector, SITE, [SITE])).toEqual({ ok: false, code: 'covered' })
    }
  })

  it.each(['iframe', 'frame', 'object', 'embed', 'span'])('checks the deepest open-shadow hit: %s', (tag) => {
    setHtml(document.body, '<div class="skip"><outer-host></outer-host></div>')
    const host = document.querySelector('outer-host')!
    const outer = host.attachShadow({ mode: 'open' })
    const innerHost = document.createElement('inner-host')
    outer.append(innerHost)
    const inner = innerHost.attachShadow({ mode: 'open' })
    const hit = document.createElement(tag)
    inner.append(hit)
    Object.defineProperty(outer, 'elementFromPoint', { value: vi.fn(() => innerHost) })
    Object.defineProperty(inner, 'elementFromPoint', { value: vi.fn(() => hit) })
    drawnOnTop = () => host
    const expected = tag === 'span' ? { ok: true, x: 60, y: 20 } : { ok: false, code: 'covered' }
    expect(walker().recipeTarget('.skip')).toEqual(expected)
    expect(walker().recipeClick('.skip', SITE, [SITE])).toEqual(expected)
    expect(outer.elementFromPoint).toHaveBeenCalledWith(60, 20)
    expect(inner.elementFromPoint).toHaveBeenCalledWith(60, 20)
  })

  it.each(['missing', 'null', 'host'])('handles an open-root hit test returning %s', (result) => {
    setHtml(document.body, '<div class="skip"><open-host></open-host></div>')
    const host = document.querySelector('open-host')!
    const root = host.attachShadow({ mode: 'open' })
    Object.defineProperty(root, 'elementFromPoint', {
      value: result === 'missing' ? undefined : () => result === 'host' ? host : null,
    })
    drawnOnTop = () => host
    const expected = result === 'host' ? { ok: true, x: 60, y: 20 } : { ok: false, code: 'covered' }
    expect(walker().recipeTarget('.skip')).toEqual(expected)
    expect(walker().recipeClick('.skip', SITE, [SITE])).toEqual(expected)
  })

  it.each([false, true])('refuses an opaque custom-element descendant (closed root: %s)', (closed) => {
    setHtml(document.body, '<div class="skip"><opaque-host></opaque-host></div>')
    const host = document.querySelector('opaque-host')!
    if (closed) host.attachShadow({ mode: 'closed' }).append(document.createElement('iframe'))
    drawnOnTop = () => host
    expect(host.shadowRoot).toBeNull()
    expect(walker().recipeTarget('.skip')).toEqual({ ok: false, code: 'covered' })
    expect(walker().recipeClick('.skip', SITE, [SITE])).toEqual({ ok: false, code: 'covered' })
    expect(walker().recipeTarget('opaque-host')).toEqual({ ok: true, x: 60, y: 20 })
  })

  it('clicks the matched element itself and answers its centre', () => {
    setHtml(document.body, '<button class="skip"><span>Skip</span></button>')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeClick('.skip', SITE, [SITE])).toEqual({ ok: true, x: 60, y: 20 })
    expect(onClick).toHaveBeenCalledTimes(1)
    // A script click: the site sees isTrusted false.
    expect(onClick.mock.calls[0][0].isTrusted).toBeFalsy()
  })

  it('sends the click to the element, not to what is drawn at its point', () => {
    setHtml(document.body, '<button class="skip">Skip</button><iframe class="late"></iframe>')
    const onSkip = vi.fn()
    const onFrame = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onSkip)
    document.querySelector('.late')!.addEventListener('click', onFrame)
    // The point checks pass; the click is bound to the element, so a frame
    // the page moves to that point afterwards never receives it.
    expect(walker().recipeClick('.skip', SITE, [SITE]).ok).toBe(true)
    expect(onSkip).toHaveBeenCalledTimes(1)
    expect(onFrame).not.toHaveBeenCalled()
  })

  it.each([
    ['covered', '<button class="skip">Skip</button><div class="modal">Sign in</div>'],
    ['hidden', '<button id="hidden" class="skip">Skip</button>'],
  ])('clicks nothing when the element is %s', (code, html) => {
    setHtml(document.body, html)
    if (code === 'covered') drawnOnTop = () => document.querySelector('.modal')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeClick('.skip', SITE, [SITE])).toEqual({ ok: false, code })
    expect(onClick).not.toHaveBeenCalled()
  })

  it('reading a target clicks nothing', () => {
    setHtml(document.body, '<button class="skip">Skip</button>')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeTarget('.skip').ok).toBe(true)
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('recipeClick judges the recipe site in the same call', () => {
  it.each([
    ['https://www.youtube.com/results?q=x', 'https://www.youtube.com/watch'],
    ['https://www.youtube.com/watch', 'https://m.youtube.com/watch'],
    ['https://www.youtube.com:8443/watch', 'https://www.youtube.com/watch'],
    ['https://www.youtube.com/watch%2Fx', 'https://www.youtube.com/watch*'],
    ['https://www.youtube.com/video/BVx', 'https://www.youtube.com/video/BV*/end'],
  ])('clicks nothing on %s for site %s', (url, site) => {
    vi.stubGlobal('location', new URL(url))
    setHtml(document.body, '<button class="skip">Skip</button>')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeClick('.skip', site, [site])).toEqual({ ok: false, code: 'wrong_site' })
    expect(onClick).not.toHaveBeenCalled()
  })

  it.each([
    ['https://www.youtube.com/watch?v=1', 'https://www.youtube.com/watch'],
    ['https://www.bilibili.com/video/BV1x', 'https://www.bilibili.com/video/BV*'],
    ['https://a.example.com/x/mid/y/end', 'https://a.example.com/x/*/y/*end'],
  ])('clicks on %s for site %s', (url, site) => {
    vi.stubGlobal('location', new URL(url))
    setHtml(document.body, '<button class="skip">Skip</button>')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeClick('.skip', site, [site]).ok).toBe(true)
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('recipeClick judges the session sites in the same call', () => {
  it.each([
    ['no session sites', undefined],
    ['an empty list', []],
    ['only another page', ['https://www.youtube.com/results']],
  ])('clicks nothing with %s', (_label, sites) => {
    setHtml(document.body, '<button class="skip">Skip</button>')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeClick('.skip', SITE, sites)).toEqual({ ok: false, code: 'wrong_site' })
    expect(onClick).not.toHaveBeenCalled()
  })

  it('clicks when one session site and the recipe site match', () => {
    setHtml(document.body, '<button class="skip">Skip</button>')
    const onClick = vi.fn()
    document.querySelector('.skip')!.addEventListener('click', onClick)
    expect(walker().recipeClick('.skip', SITE, ['https://www.youtube.com/results', SITE]).ok).toBe(true)
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
