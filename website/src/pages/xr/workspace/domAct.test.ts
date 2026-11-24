import { describe, expect, it, vi } from 'vitest'
import { act, scrollAt } from './domAct'

/** A page's document whose point (10, 10) is the element `at` (this test's DOM lays nothing out). */
function pageWith(html: string, at: string): { doc: Document; el: HTMLElement } {
  const doc = document.implementation.createHTMLDocument('page')
  doc.body.innerHTML = html
  const el = doc.querySelector(at) as HTMLElement
  Object.defineProperty(doc, 'elementFromPoint', { value: () => el })
  return { doc, el }
}

describe('a press handed by script into a page drawn by the tab', () => {
  it('presses, lets go and clicks the control under the point, as a mouse does, and gives a field the focus', () => {
    const { doc, el } = pageWith('<label>name <input id="f"></label>', '#f')
    const seen: string[] = []
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) el.addEventListener(type, ev => { seen.push(`${type}:${(ev as MouseEvent).clientX},${(ev as MouseEvent).clientY}`) })
    expect(act(doc, 10, 12, 'down')).toBe(el)
    expect(act(doc, 10, 12, 'click')).toBe(el)
    expect(seen).toEqual(['pointerdown:10,12', 'mousedown:10,12', 'pointerup:10,12', 'mouseup:10,12', 'click:10,12'])
    // The field has the focus from the press, before the release, as with a real mouse.
    expect(doc.activeElement).toBe(el)
  })

  it('runs a button\'s own action, and a drag that ends sends no click', () => {
    const { doc, el } = pageWith('<button id="b">Allow once</button>', '#b')
    const clicked = vi.fn()
    el.addEventListener('click', clicked)
    act(doc, 5, 5, 'down')
    act(doc, 5, 5, 'up')
    expect(clicked).not.toHaveBeenCalled()
    act(doc, 5, 5, 'down')
    act(doc, 5, 5, 'click')
    expect(clicked).toHaveBeenCalledTimes(1)
  })

  it('sends the release and its click to the control that took the down, wherever the release is', () => {
    const doc = document.implementation.createHTMLDocument('page')
    doc.body.innerHTML = '<button id="a">Allow once</button><button id="b">Deny</button>'
    const a = doc.getElementById('a') as HTMLElement
    const b = doc.getElementById('b') as HTMLElement
    let under: Element = a
    Object.defineProperty(doc, 'elementFromPoint', { value: () => under })
    const hit = { a: vi.fn(), b: vi.fn() }
    a.addEventListener('click', hit.a)
    b.addEventListener('click', hit.b)
    const took = act(doc, 5, 5, 'down')
    under = b
    expect(act(doc, 60, 5, 'click', took)).toBe(a)
    expect(hit.a).toHaveBeenCalledTimes(1)
    expect(hit.b).not.toHaveBeenCalled()
  })

  it('does nothing where the page has nothing', () => {
    const doc = document.implementation.createHTMLDocument('page')
    Object.defineProperty(doc, 'elementFromPoint', { value: () => null })
    expect(act(doc, 5, 5, 'click')).toBeNull()
  })

  it('scrolls the nearest box under the point that scrolls, and the page when none does', () => {
    const { doc, el } = pageWith('<div id="s" style="overflow-y:auto"><p id="p">text</p></div>', '#p')
    const box = doc.getElementById('s') as HTMLElement
    let tall = 900
    Object.defineProperty(box, 'scrollHeight', { get: () => tall })
    Object.defineProperty(box, 'clientHeight', { value: 300 })
    const boxScroll = vi.fn()
    box.scrollBy = boxScroll as unknown as typeof box.scrollBy
    expect(scrollAt(doc, 5, 5, 0, 120)).toBe(box)
    expect(boxScroll).toHaveBeenCalledWith({ left: 0, top: 120 })
    tall = 300
    const top = doc.scrollingElement as HTMLElement
    const topScroll = vi.fn()
    top.scrollBy = topScroll as unknown as typeof top.scrollBy
    expect(scrollAt(doc, 5, 5, 0, -40)).toBe(top)
    expect(topScroll).toHaveBeenCalledWith({ left: 0, top: -40 })
    expect(el).toBeTruthy()
  })
})
