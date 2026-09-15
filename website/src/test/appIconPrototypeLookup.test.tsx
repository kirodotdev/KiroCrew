/**
 * `appIcon` resolves an app-declared icon NAME, which is unvalidated third-party
 * manifest data, so it must fall back rather than throw. The trap this pins: the
 * lookup table is an object literal, so a bare `TABLE[name]` finds inherited
 * `Object.prototype` members — `TABLE['toString']` is a truthy FUNCTION, which a
 * `(name && TABLE[name]) || fallback` expression returns instead of the fallback,
 * and React throws "Functions are not valid as a React child" on it. An app
 * declaring `"icon": "toString"` would take down the surface rendering its row.
 */
import { describe, it, expect } from 'vitest'
import { isValidElement } from 'react'
import { render } from '@testing-library/react'
import { appIcon, APP_ICON_NAMES } from '../apps/appIcons'

/** Inherited members an object literal exposes through its prototype chain. */
const PROTOTYPE_MEMBERS = [
  'toString',
  'valueOf',
  'constructor',
  'hasOwnProperty',
  'isPrototypeOf',
  'propertyIsEnumerable',
  'toLocaleString',
  '__proto__',
  '__defineGetter__',
] as const

describe('appIcon', () => {
  it('returns a renderable element for every documented icon name', () => {
    for (const name of APP_ICON_NAMES) {
      expect(isValidElement(appIcon(name)), name).toBe(true)
    }
  })

  it('falls back for an Object.prototype member instead of returning it', () => {
    const fallback = appIcon('definitely-not-an-icon')
    for (const name of PROTOTYPE_MEMBERS) {
      const got = appIcon(name)
      expect(isValidElement(got), name).toBe(true)
      // Same element type as the miss case — the generic glyph, not a function or
      // an object smuggled out of the prototype chain.
      expect(got.type, name).toBe(fallback.type)
    }
  })

  it('renders a prototype-member name without throwing', () => {
    // The end-to-end statement of the bug: React is what actually throws on a
    // function child, so a type-level assertion alone would not have caught it.
    for (const name of PROTOTYPE_MEMBERS) {
      expect(() => render(<span>{appIcon(name)}</span>).unmount(), name).not.toThrow()
    }
  })

  it('falls back for an absent or empty name', () => {
    expect(isValidElement(appIcon(undefined))).toBe(true)
    expect(isValidElement(appIcon(''))).toBe(true)
    expect(appIcon('').type).toBe(appIcon('definitely-not-an-icon').type)
  })
})
