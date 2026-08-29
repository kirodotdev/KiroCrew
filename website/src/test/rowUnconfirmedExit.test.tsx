/** An unconfirmed row must stay removable after the composer strip is gone.
 *
 *  Editing past containment retires the strip while the row's `deliveryUnknown` deliberately stays --
 *  an edit is no receipt, so un-dimming would make the transcript vouch for an unproven delivery. But
 *  `dropUnconfirmedRow` was reachable only from a strip exit, so once the strip went the bubble was
 *  permanent: dimmed, captioned, and with no action beside it.
 *
 *  The exit is a HOST-SUPPLIED callback, not a store read: this row is also drawn by the app-sdk
 *  renderers and the IME suites with no Provider around it, so touching the store here breaks them.
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render, fireEvent } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import { i18nT } from '../i18n/t'

const renderRow = (meta: Record<string, unknown>, onRemoveUnconfirmed?: (id: string) => void) =>
  render(
    <UserMessage
      content="the unconfirmed send"
      meta={meta}
      renderContent={(c: string) => <span>{c}</span>}
      onRemoveUnconfirmed={onRemoveUnconfirmed}
    />,
  )

describe('an unconfirmed row carries its own removal exit', () => {
  it('offers the exit on a row whose delivery is unknown', () => {
    const { container } = renderRow({ deliveryUnknown: true, sendId: 's-orphan' }, vi.fn())

    const exit = container.querySelector('[data-testid="row-exit-remove"]')
    expect(exit, 'an edit retires the strip, so the row needs an exit of its own').not.toBeNull()
    expect(exit?.textContent?.trim()).toBe(i18nT('pages.chatPage.delivery_remove'))
  })

  it('names THIS send when the exit is used', () => {
    const onRemove = vi.fn()
    const { container } = renderRow({ deliveryUnknown: true, sendId: 's-orphan' }, onRemove)

    fireEvent.click(container.querySelector('[data-testid="row-exit-remove"]')!)

    expect(onRemove, 'the host drops the row by the send the caption stands for')
      .toHaveBeenCalledWith('s-orphan')
  })

  it('offers NO exit once delivery is confirmed', () => {
    // Positive control: an exit on a delivered row would invite deleting a real turn.
    const { container } = renderRow(
      { deliveryUnknown: true, deliveryConfirmed: true, sendId: 's-ok' }, vi.fn())

    expect(container.querySelector('[data-testid="row-exit-remove"]'),
      'a confirmed row is not a phantom').toBeNull()
  })

  it('offers NO exit when the row names no send', () => {
    // Second positive control: without a sendId the drop cannot be addressed, and drawing a control
    // that cannot act is worse than drawing none.
    const { container } = renderRow({ deliveryUnknown: true }, vi.fn())

    expect(container.querySelector('[data-testid="row-exit-remove"]')).toBeNull()
  })

  it('is supplied by BOTH chat hosts, so neither surface keeps a stuck bubble', () => {
    // The unit tests above prove the row DRAWS an exit it is handed; only reading the hosts proves
    // they hand one over. A host that dropped the prop would keep the defect with every test green.
    const pane = readFileSync(resolve(__dirname, '../components/ChatPane.tsx'), 'utf8')
    const page = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

    expect(pane, 'the pane transcript must offer the row exit').toContain('onRemoveUnconfirmed:')
    expect(page, 'the main chat must offer the row exit').toContain('onRemoveUnconfirmed={')
    for (const [name, src] of [['pane', pane], ['page', page]] as const) {
      expect(src.includes('dropUnconfirmedRow'),
        `${name} must actually drop the row, not merely hide the caption`).toBe(true)
    }
  })

  it('has each host KEEP the text its label promises, not just splice the row', () => {
    // `dropUnconfirmedRow` only splices, so a handler wired straight to it deletes the message's
    // last copy while the label says "keep the text" -- and the composer no longer holds it.
    const pane = readFileSync(resolve(__dirname, '../components/ChatPane.tsx'), 'utf8')
    const page = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

    for (const [name, src] of [['pane', pane], ['page', page]] as const) {
      const handler = src.match(/const removeUnconfirmedRow = useCallback\([\s\S]*?\n  \}, \[[^\]]*\]\)/)?.[0]
      expect(handler, `${name} must own a row-exit handler`).toBeTruthy()
      expect(handler, `${name} must merge the row text back before dropping it`)
        .toContain('mergeRecoveredDraft')
      expect(handler, `${name} must still drop the row`).toContain('dropUnconfirmedRow')
      // The reducer refuses a delivered row, so merging one that SURVIVES would seat a duplicate.
      expect(handler, `${name} must not merge a row that will survive`)
        .toContain('deliveryConfirmed')
    }
  })

  it('offers NO exit to a host that supplied no handler', () => {
    // Third positive control: the app-sdk draws this row with no store behind it, so an exit there
    // would be inert -- and wiring the store into the row is what broke 86 tests.
    const { container } = renderRow({ deliveryUnknown: true, sendId: 's-orphan' })

    expect(container.querySelector('[data-testid="row-exit-remove"]')).toBeNull()
  })
})
