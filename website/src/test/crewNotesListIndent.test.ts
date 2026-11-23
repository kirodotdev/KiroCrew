// Crew Notes lists indent on the items (a start margin, like the renderer's `ms-8`), not on the
// list: a list-level padding would add to the item margin and would not mirror a right-to-left item.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'

const css = readFileSync(resolve(process.cwd(), 'src/styles/crew-notes.css'), 'utf-8')
const rule = (selector: string) =>
  css.split('}').map((b) => b.split('{')).filter(([head]) => head.split('*/').pop()!.split(',').map((s) => s.trim()).join(', ') === selector).map(([, body]) => body).join('')

it('puts the Crew Notes list indent on the items, not the list', () => {
  expect(rule('.crew-notes ul, .crew-notes ol')).toContain('padding-inline-start: 0')
  expect(rule('.crew-notes ul > li, .crew-notes ol > li')).toContain('margin-inline-start: 1.25rem')
  expect(rule('.crew-notes li ul, .crew-notes li ol')).not.toContain('padding')
  expect(rule('.crew-notes li ul > li, .crew-notes li ol > li')).toContain('margin-inline-start: 1rem')
  expect(rule('.crew-notes ul:not(li ul) > li + li, .crew-notes ol:not(li ol) > li + li')).toContain('margin-top: 7px')
})
