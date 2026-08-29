/** The strip both chat surfaces show above the composer has ONE implementation now.
 *
 *  Every round of caption / affordance / dismiss-note feedback used to be applied to two hand-kept
 *  copies, so this pins the decision table once and asserts neither surface re-spells it -- a
 *  behavioural test alone cannot detect a caller drifting back to its own markup.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { DeliveryWarningStrip } from './DeliveryWarningStrip'
import { i18nT } from '../i18n/t'

const SURFACES = ['src/components/ChatPane.tsx', 'src/pages/ChatPage.tsx']
const read = (f: string) => readFileSync(resolve(process.cwd(), f), 'utf-8')
const noop = () => {}

const strip = (delivered: boolean, discardable: boolean) => render(
  <DeliveryWarningStrip
    noteId="test-note"
    delivered={delivered}
    discardable={discardable}
    onDiscard={noop}
    onDismiss={noop}
  />)

const T = (k: string) => i18nT(`pages.chatPage.${k}`) as string

describe('the delivery warning strip decides once for both surfaces', () => {
  it('offers the DISCARD exit whenever a payload is recoverable', () => {
    for (const delivered of [false, true]) {
      const { container } = strip(delivered, true)
      expect(container.querySelector('[data-testid="delivery-exit-discard"]'),
        `discardable/delivered=${delivered} must offer the discard exit`).not.toBeNull()
      expect(container.querySelector('[data-testid="delivery-exit-dismiss"]')).toBeNull()
      expect(container.querySelector('[data-testid="delivery-exit-discard"]')?.textContent?.trim())
        .toBe(delivered ? T('delivery_clear') : T('delivery_discard'))
    }
  })

  it('names the outcome and STATES the consequence only on the arm that drops a send', () => {
    const dropping = strip(false, false)
    expect(dropping.container.querySelector('[data-testid="delivery-exit-dismiss"]')?.textContent?.trim())
      .toBe(T('delivery_remove'))
    const note = dropping.container.querySelector('#test-note')
    expect(note, 'the destructive consequence is stated, not hidden').not.toBeNull()
    expect(dropping.container.querySelector('[data-testid="delivery-exit-dismiss"]')
      ?.getAttribute('aria-describedby'), 'and is wired to the control').toBe('test-note')

    // Delivered: nothing is dropped, so the generic verb is right and the note would be false.
    const settled = strip(true, false)
    expect(settled.container.querySelector('[data-testid="delivery-exit-dismiss"]')?.textContent?.trim())
      .toBe(i18nT('app.dismiss') as string)
    expect(settled.container.querySelector('#test-note')).toBeNull()
  })

  it('renders the total-loss exit at destructive weight, never as the harmless one', () => {
    // The undelivered Discard drops the row AND clears the text, so in the muted style of a harmless
    // acknowledgement it reads as costing nothing and gets clicked as if it did.
    const arm = (delivered: boolean, discardable: boolean, id: string) =>
      strip(delivered, discardable).container.querySelector(`[data-testid="${id}"]`) as HTMLElement
    const destructive = arm(false, true, 'delivery-exit-discard')
    const harmless = arm(false, false, 'delivery-exit-dismiss')
    expect(destructive.className,
      'the total-loss exit must not carry the muted treatment of a harmless one')
      .not.toContain('text-muted')
    expect(destructive.className, 'it carries the strip warn colour instead').toContain('text-warn')
    expect(harmless.className, 'and the harmless exit stays muted').toContain('text-muted')

    // Once delivery is PROVEN the words survive in the transcript, so clearing is not a loss.
    expect(arm(true, true, 'delivery-exit-discard').className,
      'a delivered clear is not destructive and must not shout')
      .toContain('text-muted')
  })

  it('renders the consequence at warn prominence, never as muted body text', () => {
    const note = strip(false, false).container.querySelector('#test-note')
    expect(note?.className, 'a destructive consequence must not be de-emphasised').not.toMatch(/text-muted/)
    expect(note?.className).toMatch(/text-warn/)
  })

  it('states the consequence in exactly the one state that drops a send', () => {
    for (const delivered of [false, true]) {
      for (const discardable of [false, true]) {
        const shown = strip(delivered, discardable).container.querySelector('#test-note') !== null
        expect(shown, `delivered=${delivered} discardable=${discardable}`)
          .toBe(!delivered && !discardable)
      }
    }
  })

  it('never labels two different outcomes with the same words', () => {
    // The recoverable arm empties the composer; the dropping arm leaves the text and removes only the
    // bubble. Sharing one label taught users to mispredict whichever they met second.
    const labels = new Set<string>()
    for (const delivered of [false, true]) {
      for (const discardable of [false, true]) {
        const { container } = strip(delivered, discardable)
        const exit = container.querySelector('[data-testid="delivery-exit-discard"]')
          ?? container.querySelector('[data-testid="delivery-exit-dismiss"]')
        const label = exit?.textContent?.trim() ?? ''
        expect(label, `delivered=${delivered} discardable=${discardable} renders an exit`).not.toBe('')
        labels.add(label)
      }
    }
    expect(labels.size, 'four distinct outcomes need four distinct labels').toBe(4)
  })

  it('is not re-spelled by either surface', () => {
    for (const f of SURFACES) {
      const src = read(f)
      expect(src.includes('<DeliveryWarningStrip'), `${f} renders the shared strip`).toBe(true)
      // Only markers the strip OWNS. `delivery_unconfirmed_resend` is deliberately absent from this
      // list: a transcript notice row uses the same string, so its presence is not strip residue.
      for (const own of ['delivery-exit-discard', 'delivery-exit-dismiss',
                         'delivery_unconfirmed_dismiss_note']) {
        expect(src.includes(own),
          `${f} must not carry the strip's own ${own} -- that is how the copies drifted`).toBe(false)
      }
    }
  })
})
