/**
 * The floating thread popover holds an unsent reply, so the inputs that close
 * it must not include ones meant for something layered over it:
 *  - Escape that closes a menu (the artifact toolbar's More menu),
 *  - Escape or a backdrop click that answers a modal (the discard prompt the
 *    page shows before Send to a session drops this very reply),
 *  - a press on the toolbar's More trigger, which opens that menu.
 * A plain Escape or an outside click still closes it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import type { ArtifactComment } from '../types'

const root: ArtifactComment = {
  id: 'r', origin: 'local', scope: 'private', author: 'alex', is_agent: false,
  body: 'a thread', thread_id: 'r', status: 'open', sync_state: 'local_only',
  created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
}

function renderPopover() {
  const onClose = vi.fn()
  render(
    <div>
      <button type="button" data-ui-location="artifacts.detail.more">More actions</button>
      <div role="menu" aria-label="More"><div role="menuitem" tabIndex={-1}>Copy</div></div>
      <div data-testid="outside">page</div>
      <CommentThreadPopover
        comments={[root]} rootId="r" rect={{ x: 10, y: 10, w: 10, h: 10 }}
        onClose={onClose} onReply={vi.fn()} onResolve={vi.fn()} onMarkReview={vi.fn()}
        onReopen={vi.fn()} onDelete={vi.fn()}
      />
    </div>,
  )
  return onClose
}

/** A modal the way Modal.tsx renders one: a backdrop beside an aria-modal dialog. */
function openModal() {
  const backdrop = document.createElement('div')
  backdrop.dataset.testid = 'backdrop'
  const dialog = document.createElement('div')
  dialog.setAttribute('role', 'dialog')
  dialog.setAttribute('aria-modal', 'true')
  document.body.append(backdrop, dialog)
  return { backdrop, dialog }
}

describe('CommentThreadPopover dismissal', () => {
  afterEach(() => {
    document.querySelectorAll('[aria-modal="true"], [data-testid="backdrop"]').forEach(el => el.remove())
  })

  it('stays open when Escape closes a menu', () => {
    const onClose = renderPopover()
    fireEvent.keyDown(screen.getByRole('menuitem', { name: 'Copy' }), { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('stays open when Escape answers a modal over the page', () => {
    const onClose = renderPopover()
    const { dialog } = openModal()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('stays open when the backdrop of a modal is clicked', () => {
    const onClose = renderPopover()
    const { backdrop } = openModal()
    fireEvent.mouseDown(backdrop)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('stays open when the More trigger is pressed', () => {
    const onClose = renderPopover()
    fireEvent.mouseDown(screen.getByRole('button', { name: 'More actions' }))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('stays open when a code-block thread chip inside a shadow root is pressed', () => {
    // Pierre portals the chip into its shadow DOM; the event target the
    // document sees is the shadow HOST, so only the composed path names it.
    const onClose = renderPopover()
    const host = document.createElement('div')
    document.body.append(host)
    const shadow = host.attachShadow({ mode: 'open' })
    const chip = document.createElement('button')
    chip.setAttribute('data-mc-code-chip', '')
    const plain = document.createElement('span')
    shadow.append(chip, plain)
    chip.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, composed: true }))
    expect(onClose).not.toHaveBeenCalled()
    // Anything else in that shadow root is still an outside click.
    plain.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, composed: true }))
    expect(onClose).toHaveBeenCalledTimes(1)
    host.remove()
  })

  it('still closes on a plain Escape and on an outside click', () => {
    const onClose = renderPopover()
    fireEvent.keyDown(screen.getByTestId('outside'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.mouseDown(screen.getByTestId('outside'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
