/** The strip both chat surfaces show above the composer, and its one exit.
 *
 *  The surface test asserts neither surface re-spells the strip or its exit: a behavioural test
 *  alone cannot detect a caller drifting back to its own markup.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { DeliveryWarningStrip } from './DeliveryWarningStrip'
import { announceIfRemovedSendDelivered } from './useDeliveryWarningStrip'
import {
  deliveryWarningsFor, dismissDeliveryWarnings, pruneDeliveryWarnings, removedSendsFor, warnDeliveryUnconfirmed,
} from '../utils/deliveryWarning'
import type { AppDispatch } from '../store'
import { i18nT } from '../i18n/t'

const SURFACES = ['src/components/ChatPane.tsx', 'src/pages/ChatPage.tsx']
const read = (f: string) => readFileSync(resolve(process.cwd(), f), 'utf-8')
const T = (k: string) => i18nT(`pages.chatPage.${k}`) as string

describe('the delivery warning strip', () => {
  it('names the removal and states its consequence', () => {
    const { container } = render(<DeliveryWarningStrip noteId="test-note" onDismiss={() => {}} />)
    const exit = container.querySelector('[data-testid="delivery-exit-dismiss"]') as HTMLElement
    expect(exit.textContent?.trim()).toBe(T('delivery_remove'))
    expect(exit.getAttribute('aria-describedby')).toBe('test-note')
    expect(container.querySelector('#test-note')?.textContent).toBe(T('delivery_unconfirmed_dismiss_note'))
  })

  it('lets the caption lead: only the caption carries the warning colour', () => {
    const { container } = render(<DeliveryWarningStrip noteId="n" onDismiss={() => {}} />)
    const note = container.querySelector('#n') as HTMLElement
    expect(note.className).not.toMatch(/font-medium|font-semibold|font-bold/)
    expect(note.className).toContain('text-muted')
    expect(container.firstElementChild?.className ?? '').not.toContain('text-warn')
    expect(container.querySelector('[role="status"]')?.className ?? '').toContain('text-warn')
  })

  it('is not re-spelled by either surface', () => {
    for (const f of SURFACES) {
      const src = read(f)
      expect(src.includes('<DeliveryWarningStrip'), `${f} renders the shared strip`).toBe(true)
      expect(/useDeliveryWarningStrip(<\w+>)?\(/.test(src), `${f} takes its state from the shared hook`).toBe(true)
      // `delivery_unconfirmed_resend` is deliberately absent: a transcript notice row uses the same string.
      for (const own of ['delivery-exit-dismiss', 'delivery_unconfirmed_dismiss_note', 'mergeRecoveredDraft',
                         'dropUnconfirmedRow']) {
        expect(src.includes(own), `${f} must not carry the strip's own ${own}`).toBe(false)
      }
    }
  })
})

describe('dismissDeliveryWarnings', () => {
  let n = 0
  let slot: string
  let restored: string[]
  let dropped: string[]
  const deps = (pending: (id: string) => boolean = () => true) => ({
    stillPending: pending,
    dropRow: (id: string) => { dropped.push(id) },
    restore: (sent: string) => { restored.push(sent) },
    merge: (a: string, b: string) => `${a} + ${b}`,
  })

  beforeEach(() => {
    slot = `slot-dismiss-${n++}`
    restored = []
    dropped = []
    warnDeliveryUnconfirmed(slot, { sendId: 's1', surface: 'page', epoch: 0, sent: 'run tests' })
  })

  it('hands the send to the restore of the surface that raised it and removes its row', () => {
    dismissDeliveryWarnings<string>(slot, 'page', deps())
    expect(restored).toEqual(['run tests'])
    expect(dropped).toEqual(['s1'])
    expect(deliveryWarningsFor(slot)).toHaveLength(0)
    expect(removedSendsFor(slot)).toEqual(['s1'])
  })

  it('leaves the composer and the row alone for a send no longer pending at click time', () => {
    dismissDeliveryWarnings<string>(slot, 'page', deps(() => false))
    expect(restored).toEqual([])
    expect(dropped).toEqual([])
    expect(removedSendsFor(slot)).toEqual([])
  })

  it('never hands a send to the other surface, whose restore cannot take what it staged', () => {
    dismissDeliveryWarnings<string>(slot, 'pane', deps())
    expect(restored).toEqual([])
    expect(dropped).toEqual([])
    expect(deliveryWarningsFor(slot).map(r => r.sendId)).toEqual(['s1'])
  })

  it('keeps the record of the other surface when one surface dismisses its own', () => {
    warnDeliveryUnconfirmed(slot, { sendId: 'p1', surface: 'pane', epoch: 0, sent: 'from the pane' })
    dismissDeliveryWarnings<string>(slot, 'page', deps())
    expect(restored).toEqual(['run tests'])
    expect(deliveryWarningsFor(slot).map(r => r.sendId)).toEqual(['p1'])
  })

  it('keeps a first doubtful send when a second arrives, and restores both in one call', () => {
    warnDeliveryUnconfirmed(slot, { sendId: 's2', surface: 'page', epoch: 0, sent: 'second' })
    expect(deliveryWarningsFor(slot).map(r => r.sendId)).toEqual(['s1', 's2'])
    dismissDeliveryWarnings<string>(slot, 'page', deps())
    expect(restored).toEqual(['run tests + second'])
    expect(dropped).toEqual(['s1', 's2'])
  })
})

describe('warning lifecycle', () => {
  it('prunes a record whose row left the transcript, as a /clear does', () => {
    warnDeliveryUnconfirmed('slot-prune', { sendId: 'p1', surface: 'page', epoch: 0, sent: 'x' })
    pruneDeliveryWarnings('slot-prune', () => false)
    expect(deliveryWarningsFor('slot-prune')).toHaveLength(0)
  })

  it('says so once when a removed send turns out to have been delivered', () => {
    const sent: unknown[] = []
    const dispatch = ((a: unknown) => { sent.push(a); return a }) as unknown as AppDispatch
    warnDeliveryUnconfirmed('slot-late', { sendId: 'l1', surface: 'pane', epoch: 0, sent: 'x' })
    dismissDeliveryWarnings('slot-late', 'pane', { stillPending: () => true, dropRow: () => {}, restore: () => {}, merge: a => a })
    announceIfRemovedSendDelivered(dispatch, 'slot-late', 'l1')
    announceIfRemovedSendDelivered(dispatch, 'slot-late', 'l1')
    expect(sent).toHaveLength(1)
    expect(JSON.stringify(sent[0])).toContain(T('delivery_removed_delivered'))
  })

  it('stays silent for a delivered send the user never removed', () => {
    const sent: unknown[] = []
    const dispatch = ((a: unknown) => { sent.push(a); return a }) as unknown as AppDispatch
    announceIfRemovedSendDelivered(dispatch, 'slot-quiet', 'q1')
    expect(sent).toHaveLength(0)
  })
})
