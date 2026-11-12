/**
 * The screenshot guard counts every cross-origin frame, hidden or not.
 *
 * With no live view to look inside a cross-origin frame, the gateway refuses a
 * screenshot while `unreadableFrames()` is not 0. A hidden frame counts too: a
 * page can show it for the moment the image is taken and hide it again before
 * either check. `object` and `embed` count like frames.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'
import { setHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

const walker = () => new Function(`return (${walkerSource()})`)() as { unreadableFrames: () => number }

/** Make every element of *tag* look cross-origin (no readable document) and give it a box. */
function crossOrigin(el: Element, box: { width: number; height: number }): void {
  Object.defineProperty(el, 'contentDocument', { get: () => null, configurable: true })
  el.getBoundingClientRect = () =>
    ({ x: 0, y: 0, top: 0, left: 0, right: box.width, bottom: box.height, ...box, toJSON: () => ({}) }) as DOMRect
}

afterEach(() => {
  setHtml(document.body, '')
})

describe('unreadableFrames', () => {
  it('counts a visible cross-origin iframe', () => {
    setHtml(document.body, '<iframe id="f"></iframe>')
    crossOrigin(document.getElementById('f')!, { width: 300, height: 200 })
    expect(walker().unreadableFrames()).toBe(1)
  })

  it('counts a cross-origin frame with no box', () => {
    setHtml(document.body, '<iframe id="f"></iframe>')
    crossOrigin(document.getElementById('f')!, { width: 0, height: 0 })
    expect(walker().unreadableFrames()).toBe(1)
  })

  it('counts a cross-origin frame hidden by CSS', () => {
    setHtml(document.body, '<iframe id="f" style="visibility:hidden"></iframe>')
    const f = document.getElementById('f')!
    crossOrigin(f, { width: 300, height: 200 })
    f.checkVisibility = () => false
    expect(walker().unreadableFrames()).toBe(1)
  })

  it('counts a visible cross-origin object and embed like a frame', () => {
    setHtml(document.body, '<object id="o"></object><embed id="e">')
    crossOrigin(document.getElementById('o')!, { width: 100, height: 100 })
    crossOrigin(document.getElementById('e')!, { width: 100, height: 100 })
    expect(walker().unreadableFrames()).toBe(2)
  })

  it('does not count a same-origin frame it can read', () => {
    setHtml(document.body, '<iframe id="f"></iframe>')
    const f = document.getElementById('f')!
    const doc = document.implementation.createHTMLDocument('inner')
    Object.defineProperty(f, 'contentDocument', { get: () => doc, configurable: true })
    expect(walker().unreadableFrames()).toBe(0)
  })

  it('reports the frames it reached, which the gateway checks against the frame tree', () => {
    setHtml(document.body, '<iframe id="f"></iframe><div id="open"></div><div id="closed"></div>')
    crossOrigin(document.getElementById('f')!, { width: 10, height: 10 })
    const open = document.getElementById('open')!.attachShadow({ mode: 'open' })
    open.appendChild(document.createElement('iframe'))
    const closed = document.getElementById('closed')!.attachShadow({ mode: 'closed' })
    closed.appendChild(document.createElement('iframe'))
    // The closed root's frame is out of reach: 2 seen, while the browser's tree holds 3.
    expect((walker() as unknown as { framesSeen: () => number }).framesSeen()).toBe(2)
  })

  it('counts a readable object document, which the field counts do not walk into', () => {
    setHtml(document.body, '<object id="o"></object>')
    const o = document.getElementById('o')!
    const doc = document.implementation.createHTMLDocument('inner')
    Object.defineProperty(o, 'contentDocument', { get: () => doc, configurable: true })
    expect(walker().unreadableFrames()).toBe(1)
  })

  it('counts nothing on a page with no frames', () => {
    setHtml(document.body, '<div><input type="text"></div>')
    expect(walker().unreadableFrames()).toBe(0)
  })

  it('marks a filled secret-named field as secret in activeField, so copy keys are refused', () => {
    setHtml(document.body, '<input id="otp" name="otp"><input id="mail" name="email">')
    const otp = document.getElementById('otp') as HTMLInputElement
    otp.value = '123456'
    otp.focus()
    const api = walker() as unknown as { activeField: () => { secret: boolean } }
    expect(api.activeField().secret).toBe(true)
    const mail = document.getElementById('mail') as HTMLInputElement
    mail.value = 'a@b.c'
    mail.focus()
    expect(api.activeField().secret).toBe(false)
  })
})
