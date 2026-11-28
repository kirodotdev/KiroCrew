/**
 * A control drawn only on mouse hover must also show on keyboard focus.
 *
 * `opacity-0 group-hover:opacity-100` hides a control until the pointer is over
 * its row. Tab still reaches it, so a keyboard user lands on something they
 * cannot see: they hear a name but see no focus ring, and cannot tell where
 * focus went. The rows that do this right add a focus reveal beside the hover
 * one (`focus-visible:opacity-100` on the control itself, or
 * `has-[:focus-visible]:opacity-100` on a wrapper that holds it, which unlike
 * `focus-within` stays hidden after a mouse click hands focus back).
 *
 * Every reveal value counts (`group-hover:opacity-40` too), and so does a
 * breakpoint reveal (`md:opacity-0 md:group-hover:opacity-100`): its focus
 * reveal must sit at the same breakpoint, or at none, and be at least as
 * visible. A `Btn`, `Clickable` or `*Button` component is a control like a
 * DOM `<button>`.
 * An adopted app's wheel copy (`src/kiro_crew/apps/adoption-pins.json`)
 * is skipped: its source of truth is the app's own repository, so a fix there
 * lands in that repository, not here.
 *
 * Scanned by walking each opening tag in the source. Any other component (a
 * PascalCase tag) and a self-closing element are skipped: they are decoration,
 * not something Tab can land on.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, resolve, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const posix = (p: string) => p.split('\\').join('/')
const SRC = posix(resolve(dirname(fileURLToPath(import.meta.url)), '..'))
const PINS = resolve(SRC, '..', '..', 'src', 'kiro_crew', 'apps', 'adoption-pins.json')

/** The frontend wheel-copy trees of adopted apps, the same dirs PR Hygiene's
 *  `adopted_wheel_copy_check.py` reads off the pins. */
const ADOPTED = (JSON.parse(readFileSync(PINS, 'utf-8')) as { pins: { name: string }[] }).pins
  .map(p => `${SRC}/apps/${p.name}/`)

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = `${dir}/${name}`
    if (statSync(p).isDirectory()) return name === 'test' || name === 'node_modules' ? [] : walk(p)
    return p.endsWith('.tsx') && !p.includes('.test.') && !ADOPTED.some(d => p.startsWith(d)) ? [p] : []
  })
}

const isComponentControl = (name: string) => name === 'Btn' || name === 'Clickable' || /^[A-Z]\w*Button$/.test(name)
const isControl = (name: string, tag: string) =>
  name === 'button' || name === 'a' || isComponentControl(name) || /\btabIndex=/.test(tag)

/** A wrapper's body holds a control: a native one, or a component whose name
 *  says it is one (a menu or a button). */
const holdsControl = (body: string) => /<(button|a)\b/.test(body) || /<(Btn|Clickable|[A-Z]\w*(Menu|Button))\b/.test(body)

/** The text between an element's opening tag (ending at *from*) and its own
 *  closing tag, found by counting nested tags of the same name. */
function bodyOf(text: string, name: string, from: number): string {
  let depth = 1
  let k = from
  while (depth > 0) {
    const open = text.indexOf(`<${name}`, k)
    const close = text.indexOf(`</${name}>`, k)
    if (close === -1) return ''
    if (open !== -1 && open < close && /[\s>]/.test(text[open + name.length + 1] ?? '')) {
      depth += 1
      k = open + name.length + 1
    } else {
      depth -= 1
      if (depth === 0) return text.slice(from, close)
      k = close + name.length + 3
    }
  }
  return ''
}

/** Every lowercase (DOM) or component-control opening tag in *text*, with its
 *  body and the line it starts on. Braces are tracked so a `>` inside a JSX
 *  expression does not end the tag. Any other PascalCase component is skipped:
 *  it is an icon or a widget whose own source the scan reaches. */
function openingTags(text: string): { tag: string; name: string; line: number; body: string }[] {
  const out: { tag: string; name: string; line: number; body: string }[] = []
  let i = 0
  while ((i = text.indexOf('<', i)) !== -1) {
    let n = i + 1
    while (n < text.length && /[A-Za-z0-9.]/.test(text[n])) n += 1
    const name = text.slice(i + 1, n)
    if (!name) { i += 1; continue }
    let depth = 0
    let j = n
    for (; j < text.length; j += 1) {
      const c = text[j]
      if (c === '{') depth += 1
      else if (c === '}') depth -= 1
      else if (c === '>' && depth === 0) break
    }
    const tag = text.slice(i, j + 1)
    if (/^[a-z]/.test(name) || isComponentControl(name)) {
      const body = tag.endsWith('/>') ? '' : bodyOf(text, name, j + 1)
      out.push({ tag, name, line: text.slice(0, i).split('\n').length, body })
    }
    i = j
  }
  return out
}

/** An optional breakpoint prefix (`md:`), captured so each reveal is matched
 *  at its own breakpoint. */
const BP = '((?:sm|md|lg|xl|2xl):)?'
const HIDDEN = new RegExp(`^${BP}opacity-0$`)
const HOVER = new RegExp(`^${BP}group-hover(?:/[\\w-]+)?:opacity-(\\d+)$`)
const FOCUS = new RegExp(
  `^${BP}(?:focus-visible|focus-within|group-focus-visible|group-focus-within|has-\\[:focus-visible\\])(?:/[\\w-]+)?:opacity-(\\d+)$`)

/** Some hover reveal in *words* has no focus reveal at its breakpoint (or at
 *  none) that is at least as visible. */
function lacksFocusReveal(words: string[]): boolean {
  const reveals = (re: RegExp) => words.flatMap((w) => {
    const m = re.exec(w)
    return m ? [{ bp: m[1] ?? '', value: Number(m[2]) }] : []
  })
  const focus = reveals(FOCUS)
  return reveals(HOVER).some(h => !focus.some(f => (f.bp === '' || f.bp === h.bp) && f.value >= h.value))
}

describe('hover-only controls', () => {
  it('scans a real source tree, so the guard cannot pass vacuously', () => {
    expect(walk(SRC).length).toBeGreaterThan(100)
  })

  it('also show when the keyboard reaches them', () => {
    const offenders = walk(SRC).flatMap((file) => openingTags(readFileSync(file, 'utf-8'))
      // Only what the keyboard lands on: a control (a button, a link, anything
      // given a tabindex), or a wrapper that holds one. A plain tooltip or an
      // overlay marked pointer-events-none is decoration Tab never reaches.
      .filter(({ tag, name, body }) => !tag.endsWith('/>') && (isControl(name, tag) || holdsControl(body)))
      .filter(({ tag }) => !/\bpointer-events-none\b/.test(tag))
      .map(entry => ({ ...entry, words: entry.tag.replace(/["'`{}]/g, ' ').split(/\s+/) }))
      .filter(({ words }) => words.some(w => HIDDEN.test(w)) && lacksFocusReveal(words))
      .map(({ line }) => `${relative(SRC, file)}:${line}`))
    expect(offenders).toEqual([])
  })
})
