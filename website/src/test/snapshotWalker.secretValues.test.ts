/**
 * The screenshot guard counts filled fields the walker's secret rule names.
 *
 * A code a person types into `<input type=text name=otp>` during a takeover has
 * no quick-ask marker, yet its digits would be in the pixels. The gateway refuses
 * a screenshot while `secretValues()` is not 0. A password input renders as dots
 * and `type=hidden` never renders, so neither counts.
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

const walker = () => new Function(`return (${walkerSource()})`)() as { secretValues: () => number }

function fill(id: string, value: string, root: Document | ShadowRoot = document): void {
  const el = root.getElementById
    ? (root as Document).getElementById(id)
    : (root as ShadowRoot).querySelector(`#${id}`)
  ;(el as HTMLInputElement).value = value
}

afterEach(() => {
  setHtml(document.body, '')
})

describe('secretValues', () => {
  it('counts a filled one-time-code text field', () => {
    setHtml(document.body, '<input id="c" type="text" name="otp">')
    fill('c', '123456')
    expect(walker().secretValues()).toBe(1)
  })

  it('counts a field named by its autocomplete or id', () => {
    setHtml(
      document.body,
      '<input id="a" autocomplete="one-time-code"><input id="api_token"><textarea id="secret"></textarea>',
    )
    fill('a', '1')
    fill('api_token', 'tok')
    fill('secret', 's')
    expect(walker().secretValues()).toBe(3)
  })

  it('counts a standard one-time-code or card-code autocomplete whatever the name', () => {
    setHtml(
      document.body,
      '<input id="c" name="code" autocomplete="one-time-code"><input id="v" name="x" autocomplete="cc-csc">',
    )
    fill('c', '123456')
    fill('v', '999')
    expect(walker().secretValues()).toBe(2)
  })

  it('counts camelCase secret names', () => {
    setHtml(
      document.body,
      '<input id="otpCode"><input id="a" name="cardCvv"><input id="b" name="MFAToken">' +
        '<input id="k" name="apiKey"><input id="K" name="APIKey">',
    )
    fill('otpCode', '1')
    fill('a', '2')
    fill('b', '3')
    fill('k', '4')
    fill('K', '5')
    expect(walker().secretValues()).toBe(5)
  })

  it('does not split a plain word into a secret one', () => {
    setHtml(document.body, '<input id="a" name="topCount"><input id="b" name="tokenizer">')
    fill('a', '1')
    fill('b', '2')
    expect(walker().secretValues()).toBe(0)
  })

  it('counts a filled secret field inside a same-origin frame', () => {
    setHtml(document.body, '<iframe id="f"></iframe>')
    const doc = document.implementation.createHTMLDocument('inner')
    Object.defineProperty(document.getElementById('f')!, 'contentDocument', { get: () => doc, configurable: true })
    setHtml(doc.body, '<input id="c" name="otp">')
    ;(doc.getElementById('c') as HTMLInputElement).value = '1'
    expect(walker().secretValues()).toBe(1)
  })

  it('counts a root too deep to look into as one', () => {
    let host: Element = document.body
    for (let d = 0; d < 9; d++) {
      const div = document.createElement('div')
      host.appendChild(div)
      host = div.attachShadow({ mode: 'open' }) as unknown as Element
    }
    expect(walker().secretValues()).toBe(1)
  })

  it('leaves out an empty field, a password, a hidden input and a plain field', () => {
    setHtml(
      document.body,
      '<input id="e" name="otp"><input id="p" type="password" name="password">' +
        '<input id="h" type="hidden" name="csrf_token" value="x"><input id="q" name="email">',
    )
    fill('p', 'hunter2')
    fill('h', 'x')
    fill('q', 'me@example.com')
    expect(walker().secretValues()).toBe(0)
  })

  it('counts a filled secret field inside an open shadow root', () => {
    setHtml(document.body, '<div id="host"></div>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, '<input id="c" name="cvv">')
    fill('c', '999', root)
    expect(walker().secretValues()).toBe(1)
  })
})
