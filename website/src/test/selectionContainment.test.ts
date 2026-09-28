/**
 * `containedSelectionRange` — the containment predicate shared by the chat
 * selection toolbar, the user bubble's copy interceptor, and the spec-builder
 * document pane (#7891).
 *
 * The predicate exists because `range.commonAncestorContainer` is hoisted OUT of
 * the container when a browser normalizes a multi-click selection of the
 * container's first or last block to a boundary point just outside it. Each of
 * the three consumers wires the helper into its own surface and pins the
 * user-visible symptom; this file pins the contract itself, including the two
 * branches no single consumer exercises alone: the identity fast path and the
 * O(1) rejection tier that must run before any stringification.
 */
import { describe, it, expect, vi } from 'vitest'
import { containedSelectionRange } from '../utils/selectionContainment'

/** outer > [ box > [ first, last ], sibling ] — the shape every consumer has:
 *  a container holding blocks, with a text-bearing sibling next to it. */
function buildDom() {
  const outer = document.createElement('div')
  const box = document.createElement('div')
  const first = document.createElement('p')
  first.textContent = 'first paragraph'
  const last = document.createElement('p')
  last.textContent = 'last line'
  box.append(first, last)
  const sibling = document.createElement('div')
  sibling.textContent = 'next message text'
  outer.append(box, sibling)
  document.body.appendChild(outer)
  return { outer, box, first, last, sibling }
}

/** Index of `node` among its parent's children — the offset a boundary point
 *  just BEFORE it uses; +1 is the point just after. */
const indexIn = (parent: Node, node: Node) => Array.from(parent.childNodes).indexOf(node as ChildNode)

describe('containedSelectionRange', () => {
  it('returns the original range untouched when the ancestor check passes', () => {
    const { box, first, last } = buildDom()
    const range = document.createRange()
    range.setStart(first.firstChild!, 0)
    range.setEnd(last.firstChild!, 4)

    // Identity, not a clone: the common case must allocate nothing.
    expect(containedSelectionRange(range, box)).toBe(range)
  })

  it('accepts a selection whose END is normalized past the container, clamped to it', () => {
    const { outer, box, last } = buildDom()
    const range = document.createRange()
    range.setStart(last.firstChild!, 0)
    // Triple-click of the LAST block ends "at the start of the next block",
    // which for the last block is a position past the container in its parent.
    range.setEnd(outer, indexIn(outer, box) + 1)
    expect(box.contains(range.commonAncestorContainer)).toBe(false)

    const contained = containedSelectionRange(range, box)
    expect(contained).not.toBeNull()
    // Clamped: the returned range stops at the container, so the sibling's line
    // box and nodes stay out of anything measured or cloned from it.
    expect(contained!.endContainer).toBe(box)
    expect(contained!.toString()).toBe('last line')
  })

  it('accepts a selection whose START is normalized before the container, clamped to it', () => {
    const { outer, box, first } = buildDom()
    const range = document.createRange()
    range.setStart(outer, indexIn(outer, box))
    range.setEnd(first.firstChild!, 5)

    const contained = containedSelectionRange(range, box)
    expect(contained).not.toBeNull()
    expect(contained!.startContainer).toBe(box)
    expect(contained!.toString()).toBe('first')
  })

  it('rejects a selection whose overhang holds a sibling\'s text', () => {
    const { outer, box, last, sibling } = buildDom()
    const range = document.createRange()
    range.setStart(last.firstChild!, 0)
    range.setEnd(outer, indexIn(outer, sibling) + 1)

    // A genuine cross-container selection: the overhang is not whitespace.
    expect(containedSelectionRange(range, box)).toBeNull()
  })

  it('rejects a selection holding neither endpoint without stringifying it', () => {
    const { box, sibling } = buildDom()
    const range = document.createRange()
    range.setStart(sibling.firstChild!, 0)
    range.setEnd(sibling.firstChild!, sibling.textContent!.length)

    // One consumer mounts an instance per message, each listening on `document`,
    // so this tier must stay O(1): the expensive branch begins by cloning the
    // range, and a foreign selection must never reach it.
    const cloneSpy = vi.spyOn(Range.prototype, 'cloneRange')
    expect(containedSelectionRange(range, box)).toBeNull()
    expect(cloneSpy).not.toHaveBeenCalled()
    cloneSpy.mockRestore()
  })
})
