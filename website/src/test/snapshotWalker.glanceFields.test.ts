/**
 * The walker's `glance` (`src/kiro_crew/browser_harness/snapshot_walker.js`), the
 * page read behind a browser action's `changes` report, run against every kind of
 * field inside every text it reads.
 *
 * What a field holds is a value, whether a person typed it or the page put it
 * there, so `glance` reads nothing inside a field: not in an alert, a wrapping
 * label or a dialog heading, and not from an element that sits inside a field. An
 * invalid field is named, and its validation message is never read, since a
 * message can repeat the value in any form. The controls scan and every text read use one test of
 * "this is a field". Each case puts the same marker in the field and checks that
 * no list `glance` returns repeats it.
 */
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { readSource } from './readSource'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readSource(join(HARNESS_DIR, 'snapshot_walker.js')).split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

interface Glance {
  controls: string[]
  dialogs: string[]
  alerts: string[]
  invalid: string[]
}

const glance = (): Glance =>
  (new Function(`return (${walkerSource()})`)() as { glance: () => Glance }).glance()

const CODE = '482913'

// Each kind of field, holding the marker as its value or as its own text.
const KINDS: Record<string, string> = {
  'an input': `<input name="f" value="${CODE}">`,
  'a textarea': `<textarea name="f">${CODE}</textarea>`,
  'a select': `<select name="f"><option>${CODE}</option></select>`,
  'an editable region': `<div contenteditable="true">${CODE}</div>`,
  'a role textbox': `<div role="textbox" id="otp">${CODE}</div>`,
  'a role searchbox': `<div role="searchbox">${CODE}</div>`,
  'a role combobox': `<div role="combobox">${CODE}</div>`,
  'a role spinbutton': `<div role="spinbutton">${CODE}</div>`,
  'a role slider': `<div role="slider">${CODE}</div>`,
  'a role with a second token': `<div role="textbox custom">${CODE}</div>`,
  'a custom element with a role': `<x-code role="textbox">${CODE}</x-code>`,
  'a shadow host holding a field': `<x-field>${CODE}</x-field>`,
}

// Each text `glance` reads, wrapped around one field.
const CONTAINERS: Record<string, (field: string) => string> = {
  alert: (k) => `<div role="alert">Code sent ${k}</div>`,
  label: (k) => `<label>Code ${k}<input name="x" aria-invalid="true"></label>`,
  'dialog heading': (k) => `<div role="dialog"><h2>Verify ${k}</h2><button>OK</button></div>`,
  'aria-describedby message': (k) =>
    `<input name="x" aria-invalid="true" aria-describedby="m"><div id="m">Wrong ${k}</div>`,
  'aria-errormessage message': (k) =>
    `<input name="x" aria-invalid="true" aria-errormessage="m"><div id="m">Wrong ${k}</div>`,
}

function render(html: string): void {
  const parsed = new DOMParser().parseFromString(`<!doctype html><body>${html}</body>`, 'text/html')
  document.body.replaceChildren(...Array.from(parsed.body.childNodes))
  for (const host of document.querySelectorAll('x-field')) {
    const inner = document.createElement('input')
    inner.name = 'inner'
    host.attachShadow({ mode: 'open' }).replaceChildren(inner, document.createElement('slot'))
  }
}

const said = (g: Glance): string => JSON.stringify([g.controls, g.dialogs, g.alerts, g.invalid])

beforeEach(() => {
  // The test DOM lays nothing out: give every element a box so `visible()` sees it.
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, left: 0, top: 0, width: 100, height: 20, right: 100, bottom: 20, toJSON: () => ({}) } as DOMRect
  }
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', { configurable: true, get: () => document.body })
  // An editable region, as a browser computes it: the nearest `contenteditable` that is not "false".
  Object.defineProperty(HTMLElement.prototype, 'isContentEditable', {
    configurable: true,
    get(this: HTMLElement) {
      const owner = this.closest('[contenteditable]')
      return !!owner && owner.getAttribute('contenteditable') !== 'false'
    },
  })
})

afterEach(() => {
  document.body.replaceChildren()
})

describe('glance reads nothing a field holds', () => {
  it('a one-time code in a custom role="textbox" inside an alert stays out of the report', () => {
    render(`<div role="alert">Your code <div role="textbox" id="otp">${CODE}</div> was sent</div>`)
    const g = glance()
    expect(said(g)).not.toContain(CODE)
    expect(g.alerts).toEqual(['Your code was sent'])
  })

  for (const [container, wrap] of Object.entries(CONTAINERS)) {
    for (const [kind, field] of Object.entries(KINDS)) {
      it(`${kind} inside the ${container}`, () => {
        render(wrap(field))
        expect(said(glance())).not.toContain(CODE)
      })
    }
  }

  it('a message whose element sits inside a field reads as nothing', () => {
    render(
      `<div role="textbox"><span id="m">${CODE}</span></div>` +
        `<input name="x" aria-invalid="true" aria-describedby="m">`,
    )
    const g = glance()
    expect(said(g)).not.toContain(CODE)
    expect(g.invalid).toEqual(['x'])
  })

  it('an alert inside an editable region reads as nothing', () => {
    render(`<div contenteditable="true"><div role="alert">${CODE}</div></div>`)
    expect(glance().alerts).toEqual([])
  })
})

describe('glance still reads the page text around the fields', () => {
  it('alert, dialog and label text without a field are reported', () => {
    render(
      `<div role="alert">Saved <b>2</b> items</div>` +
        `<div role="dialog"><h2>Confirm order</h2></div>` +
        `<label>Email <input type="email" name="e" aria-invalid="true" aria-describedby="h"></label>` +
        `<div id="h">Use your work address</div><button>Pay now</button>`,
    )
    const g = glance()
    expect(g.alerts).toEqual(['Saved 2 items'])
    expect(g.dialogs).toEqual(['Confirm order'])
    expect(g.invalid).toEqual(['Email'])
    expect(g.controls).toEqual(['textbox|Email', 'button|Pay now'])
  })
})

describe('glance names an invalid field and never quotes its message', () => {
  it('a describedby message that repeats the value with doubled spaces stays out', () => {
    const typed = 'ref  77  4410'
    render(
      `<label>Reference <input name="ref" aria-invalid="true" aria-describedby="m"></label>` +
        `<div id="m">"${typed}" is not a known reference</div>`,
    )
    const input = document.querySelector('input') as HTMLInputElement
    input.value = typed
    const g = glance()
    expect(g.invalid).toEqual(['Reference'])
    expect(said(g)).not.toContain('4410')
  })

  it("an email field's own validation message that quotes its value stays out", () => {
    render(`<label>Email <input type="email" name="e" aria-invalid="true"></label>`)
    const input = document.querySelector('input') as HTMLInputElement
    input.value = 'abc'
    Object.defineProperty(input, 'validationMessage', {
      configurable: true,
      get: () => `Please include an '@' in the email address. 'abc' is missing an '@'.`,
    })
    const g = glance()
    expect(g.invalid).toEqual(['Email'])
    expect(said(g)).not.toContain('abc')
  })
})

// A GitHub classic token of the real shape, built at run time the way
// `kiro_crew.testing.synthetic_credentials.github_classic()` builds it, so no
// credential-shaped text sits in the repo. None is a real credential.
const ALNUM = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

function crc32(text: string): number {
  let crc = 0xffffffff
  for (let i = 0; i < text.length; i++) {
    crc ^= text.charCodeAt(i)
    for (let bit = 0; bit < 8; bit++) crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1
  }
  return (crc ^ 0xffffffff) >>> 0
}

function githubClassic(): string {
  const body = Array.from({ length: 30 }, (_, i) => ALNUM[(13 + i * 7) % ALNUM.length]).join('')
  let crc = crc32(body)
  let check = ''
  while (crc) {
    check = BASE62[crc % 62] + check
    crc = Math.floor(crc / 62)
  }
  return `ghp_${body}${check.padStart(6, '0')}`
}

// Every run of six characters of *token*: a list entry that holds one holds a piece of it.
const pieces = (token: string): string[] =>
  Array.from({ length: token.length - 5 }, (_, i) => token.slice(i, i + 6))

describe('glance never cuts page text before the gateway redacts it', () => {
  const token = githubClassic()

  it('builds the same token the python tests use', () => {
    expect(token).toHaveLength(40)
    expect(token.slice(-6)).toBe('3Z9e6I')
  })

  it('a button label of 49 letters and then a token arrives whole', () => {
    const label = `${'a'.repeat(49)} ${token}`
    render(`<button>${label}</button>`)
    expect(glance().controls).toEqual([`button|${label}`])
  })

  for (const lead of [49, 200, 230, 600, 630, 1000]) {
    it(`a token after ${lead} letters is read whole or left out, never cut`, () => {
      const text = `${'a'.repeat(lead)} ${token}`
      render(
        `<button>${text}</button><button aria-label="${text}">x</button>` +
          `<div role="alert">${text}</div><div role="dialog" aria-label="${text}"></div>` +
          `<input name="n" aria-invalid="true" aria-label="${text}">`,
      )
      const g = glance()
      for (const entry of [...g.controls, ...g.dialogs, ...g.alerts, ...g.invalid]) {
        if (entry.includes(token)) continue
        for (const piece of pieces(token)) expect(entry).not.toContain(piece)
      }
    })
  }

  it('text too long to read whole is left out, and a long message is still said to be there', () => {
    const text = `${'a'.repeat(1000)} ${token}`
    render(`<button>${text}</button><div role="alert">${text}</div><div role="dialog" aria-label="${text}"></div>`)
    const g = glance()
    expect(g.controls).toEqual(['button|'])
    expect(g.alerts).toEqual(['a message too long to show'])
    expect(g.dialogs).toEqual(['dialog'])
  })
})

describe('glance names a custom field the page marks invalid', () => {
  it('an editable role="textbox" marked aria-invalid is named by its label', () => {
    render(`<div role="textbox" contenteditable="true" aria-invalid="true" aria-label="Promo code">SAVE10</div>`)
    const g = glance()
    expect(g.invalid).toEqual(['Promo code'])
    expect(said(g)).not.toContain('SAVE10')
  })

  it('a secret custom field marked invalid stays out', () => {
    render(`<div role="textbox" contenteditable="true" aria-invalid="true" aria-label="Code" id="otp">${CODE}</div>`)
    expect(glance().invalid).toEqual([])
  })

  it('an element marked invalid that is not a field is not listed', () => {
    render(`<div aria-invalid="true">Notes</div><button aria-invalid="true">Send</button>`)
    expect(glance().invalid).toEqual([])
  })
})

// Every character a regular expression's `\s` matches, by code point. The walker's
// two loop helpers (`squashSpace`, `spaceWords`) must treat exactly these as space.
const SPACE_CODES = [
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0xa0, 0x1680,
  ...Array.from({ length: 11 }, (_, i) => 0x2000 + i),
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
]

// Characters that look like space but that `\s` does not match.
const NOT_SPACE_CODES = [0x180e, 0x200b, 0x200c, 0x200d, 0x2060, 0x0085, 0x001c, 0x00ad]

const hex = (c: number): string => `U+${c.toString(16).toUpperCase().padStart(4, '0')}`

// `squashSpace` is read through a control's name from `aria-label` (`clip`), and
// `spaceWords` through the ids of `aria-labelledby` (`idsText`).
function nameOf(label: string): string {
  const control = document.createElement('div')
  control.setAttribute('role', 'button')
  control.setAttribute('aria-label', label)
  document.body.replaceChildren(control)
  return glance().controls[0].slice('button|'.length)
}

function labelledBy(list: string): string {
  const control = document.createElement('div')
  control.setAttribute('role', 'button')
  control.setAttribute('aria-labelledby', list)
  const texts = ['w1', 'w2', 'w3', 'w4', 'w5', 'w6'].map((id) =>
    Object.assign(document.createElement('span'), { id, textContent: id.toUpperCase() }),
  )
  document.body.replaceChildren(control, ...texts)
  return glance().controls[0].slice('button|'.length)
}

describe('the walker treats white space as a regular expression does', () => {
  const squashed = (s: string): string => s.replace(/\s+/g, ' ').trim()
  const named = (list: string): string =>
    list
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 4)
      .map((id) => document.getElementById(id)?.textContent ?? '')
      .filter(Boolean)
      .join(' ')

  it('the table holds every BMP character a regular expression matches and none that it does not', () => {
    const matched = Array.from({ length: 0x10000 }, (_, c) => c).filter((c) => /\s/.test(String.fromCharCode(c)))
    expect([...SPACE_CODES].sort((a, b) => a - b).map(hex)).toEqual(matched.map(hex))
    for (const c of NOT_SPACE_CODES) expect(/\s/.test(String.fromCharCode(c)), hex(c)).toBe(false)
  })

  const shapes: Array<[string, (c: string) => string]> = [
    ['between words', (c) => `a${c}b`],
    ['as a run between words', (c) => `a${c}${c}${c}b`],
    ['as a leading run', (c) => `${c}${c}a b`],
    ['as a trailing run', (c) => `a b${c}${c}`],
    ['around a word', (c) => `${c}a${c}`],
    ['mixed with plain spaces', (c) => ` ${c} a ${c}${c}b ${c} `],
    ['as the whole string', (c) => c],
    ['as a run that is the whole string', (c) => `${c}${c}${c}`],
  ]

  for (const code of [...SPACE_CODES, ...NOT_SPACE_CODES]) {
    for (const [where, shape] of shapes) {
      const c = String.fromCharCode(code)
      it(`squashSpace gives what the regular expression gives for ${hex(code)} ${where}`, () => {
        const text = shape(c)
        expect(nameOf(text)).toBe(squashed(text))
      })
      it(`spaceWords gives what the regular expression gives for ${hex(code)} ${where}`, () => {
        const list = shape(c).replaceAll('a', 'w1').replaceAll('b', 'w2')
        expect(labelledBy(list)).toBe(named(list))
      })
    }
  }

  it('every white space character in one run, between and around words', () => {
    const all = SPACE_CODES.map((c) => String.fromCharCode(c)).join('')
    expect(nameOf(`${all}one${all}two${all}`)).toBe('one two')
    expect(nameOf(all)).toBe('')
    expect(labelledBy(`${all}w1${all}w2${all}w3${all}`)).toBe('W1 W2 W3')
    expect(labelledBy(all)).toBe('')
  })

  it('the empty string is no name and no ids', () => {
    expect(nameOf('')).toBe('')
    expect(labelledBy('')).toBe('')
  })

  it('a list of more than four ids names nothing, so no name is read in part', () => {
    expect(labelledBy('w1 w2\u00a0w3\u3000w4\ufeffw5 w6')).toBe('')
    expect(labelledBy('w1 w2\u00a0w3\u3000w4')).toBe('W1 W2 W3 W4')
  })

  it('characters that only look like space stay inside a word', () => {
    const odd = NOT_SPACE_CODES.map((c) => String.fromCharCode(c)).join('')
    expect(nameOf(`a${odd}b \t\u00a0 c`)).toBe(`a${odd}b c`)
  })
})

// A credential split across inline elements, as syntax highlighting renders it,
// is shown with no space inside it, so it is read with none: the gateway's
// redaction then sees it whole.
describe('glance joins inline text the way the page shows it', () => {
  const token = githubClassic()
  const split = `<code>${token.slice(0, 4)}</code><code>${token.slice(4, 20)}</code><span>${token.slice(20)}</span>`
  const entries = (g: Glance): string[] => [...g.controls, ...g.dialogs, ...g.alerts, ...g.invalid]
  const whole = (g: Glance): void => {
    for (const entry of entries(g)) {
      if (entry.includes(token)) continue
      for (const piece of pieces(token)) expect(entry).not.toContain(piece)
    }
  }

  it('a split token in an alert arrives whole', () => {
    render(`<div role="alert">Your token: ${split}</div>`)
    const g = glance()
    expect(g.alerts).toEqual([`Your token: ${token}`])
    whole(g)
  })

  it('a split token in a button name arrives whole', () => {
    render(`<button>Copy ${split}</button>`)
    const g = glance()
    expect(g.controls).toEqual([`button|Copy ${token}`])
    whole(g)
  })

  it('a split token in a field label arrives whole', () => {
    render(`<label>Key ${split} <input name="k" aria-invalid="true"></label>`)
    const g = glance()
    expect(g.invalid).toEqual([`Key ${token}`])
    whole(g)
  })

  it('a split token in a dialog title arrives whole', () => {
    render(`<div role="dialog"><h2>New key ${split}</h2><button>OK</button></div>`)
    const g = glance()
    expect(g.dialogs).toEqual([`New key ${token}`])
    whole(g)
  })

  it('a split token in an element the dialog is labelled by arrives whole', () => {
    render(`<div role="dialog" aria-labelledby="t"><p id="t">Key ${split}</p></div>`)
    expect(glance().dialogs).toEqual([`Key ${token}`])
  })

  it('a split token in flex items arrives whole', () => {
    render(`<div role="alert"><div style="display: flex">${split}</div></div>`)
    expect(glance().alerts).toEqual([token])
  })

  it('a hidden or left-out element inside a token adds no space', () => {
    render(
      `<div role="alert">${token.slice(0, 10)}<span hidden>x</span><i id="mark"></i>` +
        `<span style="display: none">y</span>${token.slice(10)}</div>`,
    )
    const script = document.createElement('script')
    script.textContent = '1'
    document.getElementById('mark')?.replaceWith(script)
    expect(document.querySelector('script')?.textContent).toBe('1')
    expect(glance().alerts).toEqual([token])
  })

  it('text the page does not draw inside a token adds nothing', () => {
    for (const value of ['hidden', 'collapse']) {
      render(
        `<div role="alert">${token.slice(0, 10)}<span style="position: absolute; visibility: ${value}">` +
          ` x\n</span>${token.slice(10)}</div>`,
      )
      expect(glance().alerts).toEqual([token])
    }
  })

  it('a drawn child of an undrawn element is read', () => {
    render(
      `<div role="alert">Key <span style="visibility: hidden">x<b style="visibility: visible">` +
        `${token.slice(0, 10)}</b>y</span>${token.slice(10)}</div>`,
    )
    expect(glance().alerts).toEqual([`Key ${token}`])
  })

  it('white space the page holds stays a space', () => {
    render(`<div role="alert"><code>Saved</code> <b>2</b>\n<i>items</i></div>`)
    expect(glance().alerts).toEqual(['Saved 2 items'])
  })

  it('a block or a line break between words is a space', () => {
    render(`<div role="alert"><p>Saved</p><p>Done</p>Next<br>Last<div>End</div></div>`)
    expect(glance().alerts).toEqual(['Saved Done Next Last End'])
  })
})

// Fields that can hold elements, with `{in}` where the elements go.
const HOLDERS: Record<string, string> = {
  'a secret role="textbox" editor': `<div role="textbox" id="otp">{in}</div>`,
  'a secret editable region': `<div contenteditable="true" autocomplete="one-time-code">{in}</div>`,
  'an editable region': `<div contenteditable="true">{in}</div>`,
  'a role combobox': `<div role="combobox">{in}</div>`,
  'a shadow host holding a field': `<x-field>{in}</x-field>`,
}

// Elements inside the field that carry the field's value in a name of their own.
const INSIDE: Record<string, string> = {
  'a chip with an aria-label': `<span role="button" tabindex="0" aria-label="${CODE}">x</span>`,
  'a chip with a title': `<span role="button" tabindex="0" title="${CODE}"></span>`,
  'an image button with alt text': `<input type="image" alt="${CODE}">`,
  'a button with its own text': `<button>${CODE}</button>`,
  'a link labelled by an element in the field': `<a href="#" aria-labelledby="c">x</a><b id="c">${CODE}</b>`,
  'a dialog with an aria-label': `<div role="dialog" aria-label="${CODE}"></div>`,
  'an invalid field with a placeholder': `<input name="n" aria-invalid="true" placeholder="${CODE}">`,
  'an invalid field with an aria-label': `<input name="n" aria-invalid="true" aria-label="${CODE}">`,
}

describe('glance reads no name from an element inside a field', () => {
  for (const [holder, wrap] of Object.entries(HOLDERS)) {
    for (const [inside, html] of Object.entries(INSIDE)) {
      it(`${inside} in ${holder}`, () => {
        render(wrap.replace('{in}', html))
        expect(said(glance())).not.toContain(CODE)
      })
    }
  }

  it('the elements of a field are left out of the lists, and the page around it is read', () => {
    render(
      `<div role="textbox" aria-label="Code"><span role="button" tabindex="0" aria-label="${CODE}">x</span></div>` +
        `<button>Verify</button><div role="dialog" aria-label="Check"></div>`,
    )
    const g = glance()
    expect(g.controls).toEqual(['textbox|Code', 'button|Verify'])
    expect(g.dialogs).toEqual(['Check'])
  })
})

describe('glance reads a name built from several elements whole or not at all', () => {
  const token = githubClassic()
  // A button labelled by *n* spans that hold *pieces*.
  function labelled(pieces: string[]): string {
    const ids = pieces.map((_, i) => `p${i}`)
    const spans = pieces.map((piece, i) => `<span id="p${i}">${piece}</span>`).join('')
    render(`<div role="button" tabindex="0" aria-labelledby="${ids.join(' ')}"></div>${spans}`)
    return glance().controls[0]
  }

  it('a token split between the fourth and the fifth of five labels is left out whole', () => {
    const control = labelled(['Your', 'token', 'is', token.slice(0, 20), token.slice(20)])
    expect(control).toBe('button|')
    for (const piece of pieces(token)) expect(control).not.toContain(piece)
  })

  it('four labels are read whole', () => {
    expect(labelled(['Copy', 'your', 'new', 'key'])).toBe('button|Copy your new key')
  })

  it('a name with a label too long to read whole is left out, not read in part', () => {
    expect(labelled(['Copy', 'a'.repeat(700)])).toBe('button|')
  })

  it('a name with a label inside a field is left out, not read in part', () => {
    render(
      `<div role="button" tabindex="0" aria-labelledby="a b"></div>` +
        `<span id="a">Code</span><div role="textbox"><span id="b">${CODE}</span></div>`,
    )
    expect(glance().controls).toContain('button|')
    expect(said(glance())).not.toContain(CODE)
  })

  it('a dialog named by too many labels falls back to its heading', () => {
    render(
      `<div role="dialog" aria-labelledby="p0 p1 p2 p3 p4"><h2>Verify</h2></div>` +
        ['a', 'b', 'c', 'd', 'e'].map((t, i) => `<span id="p${i}">${t}</span>`).join(''),
    )
    expect(glance().dialogs).toEqual(['Verify'])
  })
})
