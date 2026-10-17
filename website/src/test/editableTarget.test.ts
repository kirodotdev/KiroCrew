import { describe, it, expect } from 'vitest'
import {
  activeElementIsEditable,
  deepActiveElement,
  editableEventTarget,
  isEditableElement,
  isEditableTarget,
  isTypingElement,
} from '../utils/editableTarget'

/** The predicate that shipped before this guard existed, kept verbatim so each
 *  test can state whether it is a case the old one already handled or the case
 *  it silently got wrong. */
function legacyTargetOnlyGuard(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  const tag = el?.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || el?.isContentEditable === true
}

/**
 * A keydown as a REAL engine delivers it to a document-level listener when the
 * caret sits in a `contentEditable` node inside an open shadow root: `target` is
 * the retargeted shadow HOST, while `composedPath()[0]` is the node that holds
 * the caret.
 *
 * The retargeting has to be modelled rather than produced, because jsdom does
 * not retarget composed shadow events — measured there, a document listener
 * still sees the inner node. Chromium and Firefox both report the host.
 */
function retargetedShadowKeydown(): { event: KeyboardEvent; host: HTMLElement; inner: HTMLElement } {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = host.attachShadow({ mode: 'open' })
  const pre = document.createElement('pre')
  const inner = document.createElement('div')
  // The editor's editable content node: `contentEditable` + `role=textbox`.
  inner.setAttribute('contenteditable', 'true')
  inner.setAttribute('role', 'textbox')
  pre.appendChild(inner)
  root.appendChild(pre)

  const event = new KeyboardEvent('keydown', { key: '/', bubbles: true, composed: true })
  Object.defineProperty(event, 'target', { value: host })
  Object.defineProperty(event, 'composedPath', {
    value: () => [inner, pre, root, host, document.body, document.documentElement, document, window],
  })
  return { event, host, inner }
}

describe('isEditableElement', () => {
  it('accepts the fields that consume a printable keystroke', () => {
    for (const tag of ['input', 'textarea', 'select']) {
      expect(isEditableElement(document.createElement(tag))).toBe(true)
    }
    const ce = document.createElement('div')
    ce.setAttribute('contenteditable', 'true')
    document.body.appendChild(ce)
    expect(isEditableElement(ce)).toBe(true)
  })

  it('rejects a plain element, a non-element node and a nullish target', () => {
    expect(isEditableElement(document.createElement('div'))).toBe(false)
    expect(isEditableElement(document.createTextNode('x'))).toBe(false)
    expect(isEditableElement(document)).toBe(false)
    expect(isEditableElement(window)).toBe(false)
    expect(isEditableElement(null)).toBe(false)
    expect(isEditableElement(undefined)).toBe(false)
  })

  it('rejects an explicitly non-editable node inside an editable region', () => {
    // The editor marks annotation and deletion lines `contenteditable="false"`.
    const host = document.createElement('div')
    host.setAttribute('contenteditable', 'true')
    const off = document.createElement('div')
    off.setAttribute('contenteditable', 'false')
    host.appendChild(off)
    document.body.appendChild(host)
    expect(isEditableElement(off)).toBe(false)
  })
})

describe('isEditableTarget across a shadow boundary', () => {
  it('sees the editor behind a retargeted event, where a target-only guard cannot', () => {
    const { event, host, inner } = retargetedShadowKeydown()

    // The precondition that makes this bug possible: everything a listener can
    // read off the event itself says "not editable".
    expect(event.target).toBe(host)
    expect((event.target as HTMLElement).tagName).toBe('DIV')
    expect((event.target as HTMLElement).isContentEditable).toBe(false)
    expect(legacyTargetOnlyGuard(event.target)).toBe(false)

    expect(isEditableTarget(event)).toBe(true)
    expect(editableEventTarget(event)).toBe(inner)
  })

  it('leaves a keystroke from a non-editable shadow tree claimable', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const root = host.attachShadow({ mode: 'open' })
    const plain = document.createElement('div')
    root.appendChild(plain)
    const event = new KeyboardEvent('keydown', { key: '/', bubbles: true, composed: true })
    Object.defineProperty(event, 'target', { value: host })
    Object.defineProperty(event, 'composedPath', {
      value: () => [plain, root, host, document.body, document.documentElement, document, window],
    })

    expect(isEditableTarget(event)).toBe(false)
    expect(editableEventTarget(event)).toBeNull()
  })
})

describe('isEditableTarget in the light DOM', () => {
  it('still recognises a plain field and an inherited contentEditable region', () => {
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    expect(isEditableTarget({ target: ta, composedPath: () => [ta, document.body, document] })).toBe(true)

    const region = document.createElement('div')
    region.setAttribute('contenteditable', 'true')
    const child = document.createElement('span')
    region.appendChild(child)
    document.body.appendChild(region)
    // The span is editable by inheritance, and is also covered by the walk.
    expect(isEditableTarget({ target: child, composedPath: () => [child, region, document.body] })).toBe(true)
  })

  it('leaves the page body claimable', () => {
    expect(isEditableTarget({
      target: document.body,
      composedPath: () => [document.body, document.documentElement, document, window],
    })).toBe(false)
  })

  it('falls back to the target when the event carries no composed path', () => {
    const input = document.createElement('input')
    expect(isEditableTarget({ target: input })).toBe(true)
    expect(isEditableTarget({ target: document.createElement('div') })).toBe(false)
    expect(editableEventTarget({ target: input })).toBe(input)
  })
})

describe('isTypingElement', () => {
  it('excludes <select>, which isEditableElement includes', () => {
    const sel = document.createElement('select')
    expect(isEditableElement(sel)).toBe(true)
    expect(isTypingElement(sel)).toBe(false)
  })

  it('agrees with isEditableElement on every character-input field', () => {
    for (const tag of ['input', 'textarea']) {
      const el = document.createElement(tag)
      expect(isTypingElement(el)).toBe(true)
      expect(isEditableElement(el)).toBe(true)
    }
    const ce = document.createElement('div')
    ce.setAttribute('contenteditable', 'true')
    document.body.appendChild(ce)
    expect(isTypingElement(ce)).toBe(true)
  })
})

describe('a focused <select>', () => {
  it('is editable to the hotkey guards, and not typing to the strip', () => {
    const sel = document.createElement('select')
    const e = { target: sel, composedPath: () => [sel, document.body, document] }
    expect(isEditableTarget(e)).toBe(true)
    expect(isTypingElement(sel)).toBe(false)
  })
})

describe('deepActiveElement', () => {
  it('descends into a shadow root that document.activeElement stops at', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const root = host.attachShadow({ mode: 'open' })
    const inner = document.createElement('textarea')
    root.appendChild(inner)
    inner.focus()

    // The precondition: the outer view of focus names the host, not the field.
    expect(document.activeElement).toBe(host)
    expect(isEditableElement(document.activeElement)).toBe(false)

    expect(deepActiveElement()).toBe(inner)
    expect(activeElementIsEditable()).toBe(true)
    host.remove()
  })

  it('returns the focused element unchanged when no shadow root is involved', () => {
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    expect(deepActiveElement()).toBe(input)
    expect(activeElementIsEditable()).toBe(true)
    input.remove()
  })

  it('reports a non-editable focus as claimable', () => {
    const btn = document.createElement('button')
    document.body.appendChild(btn)
    btn.focus()
    expect(deepActiveElement()).toBe(btn)
    expect(activeElementIsEditable()).toBe(false)
    btn.remove()
  })

  it('reports a focused <select> as editable', () => {
    const sel = document.createElement('select')
    document.body.appendChild(sel)
    sel.focus()
    expect(activeElementIsEditable()).toBe(true)
    sel.remove()
  })
})
