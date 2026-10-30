/**
 * `isModalLayer`: the one modal test both swipe readers walk up with (the phone
 * drawer and the desktop trackpad swipe).
 */
import { describe, expect, it } from 'vitest'
import { isModalLayer } from './modalLayer'

/** An element of `tag` with `attrs`, built with the DOM API. */
const el = (tag: string, attrs: Record<string, string> = {}) => {
  const node = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  return node
}

describe('isModalLayer', () => {
  it('reads dialog roles, aria-modal and an open <dialog> as modal', () => {
    expect(isModalLayer(el('div', { role: 'dialog' }))).toBe(true)
    expect(isModalLayer(el('div', { role: 'alertdialog' }))).toBe(true)
    expect(isModalLayer(el('div', { 'aria-modal': 'true' }))).toBe(true)
    expect(isModalLayer(el('dialog', { open: '' }))).toBe(true)
  })

  it('leaves everything else alone, a closed <dialog> included', () => {
    expect(isModalLayer(el('div', { role: 'menu' }))).toBe(false)
    expect(isModalLayer(el('div', { 'aria-modal': 'false' }))).toBe(false)
    expect(isModalLayer(el('dialog'))).toBe(false)
    expect(isModalLayer(el('section'))).toBe(false)
  })
})
