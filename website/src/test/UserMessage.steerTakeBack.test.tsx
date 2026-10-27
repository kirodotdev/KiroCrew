import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import { revealComposer } from '../pages/chat/composerFocus'

vi.mock('../pages/chat/composerFocus', async importOriginal => ({
  ...(await importOriginal<typeof import('../pages/chat/composerFocus')>()),
  revealComposer: vi.fn(),
}))

const renderContent = (content: string) => <span data-testid="content">{content}</span>

const pending = { steer: true, steerState: 'written', steerRetract: true, mid: 'm-1' }

const openMore = () => fireEvent.pointerDown(screen.getByTestId('steer-take-back-more'), { button: 0, ctrlKey: false, pointerType: 'mouse' })

function renderSteer(meta: Record<string, unknown>, props: Partial<Parameters<typeof UserMessage>[0]> = {}) {
  return render(
    <UserMessage content="use the blue fleet" meta={meta} renderContent={renderContent} slotRunning {...props} />,
  )
}

describe('taking back a steer the running turn has not read', () => {
  it('keeps the status explainer outside the two-control take-back group', () => {
    renderSteer(pending, { onSteerAction: vi.fn() })

    const status = screen.getByTestId('steer-pending-status')
    const actions = screen.getByTestId('steer-take-back')
    expect(within(actions).getAllByRole('button')).toHaveLength(2)
    expect(within(status).getByRole('button', { name: 'More information' })).toBeTruthy()
    expect(within(actions).queryByRole('button', { name: 'More information' })).toBeNull()
    expect(status.parentElement).toBe(actions.parentElement)
    expect(actions.className).toContain('border-l')
  })

  it('offers Queue instead in the row and Edit and Unsend one menu deep', () => {
    const onSteerAction = vi.fn()
    renderSteer(pending, { onSteerAction })

    const controls = screen.getByTestId('steer-take-back').querySelectorAll(':scope > button')
    expect(Array.from(controls).map(b => b.textContent || b.getAttribute('aria-label'))).toEqual(['Queue instead', 'Edit or unsend'])
    fireEvent.click(screen.getByTestId('steer-queue-instead'))
    openMore()
    fireEvent.click(screen.getByTestId('steer-edit'))
    openMore()
    fireEvent.click(screen.getByTestId('steer-unsend'))

    expect(onSteerAction.mock.calls).toEqual([['m-1', 'queue'], ['m-1', 'edit'], ['m-1', 'unsend']])
    expect(screen.getByTestId('content').textContent).toBe('use the blue fleet')
  })

  it('labels the Edit menu item with where the text goes', () => {
    renderSteer(pending, { onSteerAction: vi.fn() })

    openMore()
    expect(screen.getByTestId('steer-edit').textContent?.trim()).toBe('Edit in message box')
  })

  it('renders each take-back control through the shared Btn primitive', () => {
    const view = renderSteer(pending, { onSteerAction: vi.fn() })

    expect(screen.getByTestId('steer-queue-instead').className).toContain('active:scale-[0.97]')
    expect(screen.getByTestId('steer-take-back-more').className).toContain('active:scale-[0.97]')

    view.unmount()
    render(<UserMessage content="" meta={{ steer: true, steerState: 'retracted', steerRetractAction: 'edit', mid: 'm-1' }} renderContent={renderContent} />)
    expect(screen.getByTestId('steer-retracted').className).toContain('active:scale-[0.97]')
  })

  it.each([
    ['the row is not marked as one', { ...pending, steerRetract: undefined }, {}],
    ['the agent refused', { ...pending, steerRetract: false }, {}],
    ['the turn read it', { ...pending, steerState: 'consumed' }, {}],
    ['the turn ended first', { ...pending, steerState: 'requeued' }, {}],
    ['the row has no id', { ...pending, mid: undefined }, {}],
    ['no turn is running', pending, { slotRunning: false }],
    ['the host offers no take-back', pending, { onSteerAction: undefined }],
  ])('draws no controls when %s', (_why, meta, extra) => {
    renderSteer(meta, { onSteerAction: vi.fn(), ...extra })

    expect(screen.queryByTestId('steer-take-back')).toBeNull()
  })

  it('replaces the controls with a progress line while a take-back is in flight', () => {
    renderSteer(pending, { onSteerAction: vi.fn(), steerActionPending: 'edit', steerActionRefusal: { refusal: 'busy', action: 'edit' } })

    expect(screen.queryByTestId('steer-take-back')).toBeNull()
    expect(screen.getByRole('status').textContent).toContain('Taking it back')
    expect(screen.queryByRole('alert'), 'an earlier refusal is not shown over a new attempt').toBeNull()
  })

  it.each([
    ['unsend', "Couldn't confirm it was unsent. This updates when the agent reaches it."],
    ['edit', "Couldn't confirm it moved back to your message box. This updates when the agent reaches it."],
    ['queue', "Couldn't confirm it moved to the queue. This updates when the agent reaches it."],
  ] as const)('shows an unconfirmed %s request failure under the progress line until the take-back settles', (action, text) => {
    const onSteerAction = vi.fn()
    const view = renderSteer(pending, {
      onSteerAction,
      steerActionPending: action,
      steerActionRequestFailed: true,
    })

    expect(screen.getByRole('status').textContent).toContain('Taking it back')
    expect(screen.getByRole('alert').textContent).toBe(text)

    view.rerender(
      <UserMessage
        content="use the blue fleet"
        meta={{ ...pending, steerState: 'consumed' }}
        renderContent={renderContent}
        slotRunning
        onSteerAction={onSteerAction}
      />,
    )

    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('marks the taking-back line with the Undo icon, not the Steering target', () => {
    renderSteer(pending, { onSteerAction: vi.fn(), steerActionPending: 'queue' })

    const line = screen.getByRole('status')
    expect(line.querySelector('svg.lucide-target')).toBeNull()
    expect(line.querySelector('svg[class*="lucide-undo"]')).not.toBeNull()
  })

  it.each([
    ['busy', 'queue', 'Another message is still on its way. Try again.'],
    ['unsupported', 'unsend', "This agent can't unsend it."],
    ['unsupported', 'edit', "This agent can't move it back to your message box."],
    ['unsupported', 'queue', "This agent can't move it to the queue."],
    ['failed', 'unsend', "Couldn't unsend it."],
    ['failed', 'edit', "Couldn't move it back to your message box."],
    ['failed', 'queue', "Couldn't move it to the queue."],
  ] as const)('says why a %s %s take-back did not happen, naming that action', (refusal, action, text) => {
    renderSteer(pending, { onSteerAction: vi.fn(), steerActionRefusal: { refusal, action } })

    expect(screen.getByRole('alert').textContent).toBe(text)
  })

  it('shows no error notice while nothing was refused', () => {
    renderSteer(pending, { onSteerAction: vi.fn() })

    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each([
    ['unsend', 'You unsent this message'],
    ['edit', 'You moved this message back to your message box'],
    ['queue', 'You moved this message to the queue'],
  ])('keeps a %s take-back as a note without the bubble', (action, note) => {
    renderSteer({ steer: true, steerState: 'retracted', steerRetractAction: action, mid: 'm-1' })

    expect(screen.getByTestId('steer-retracted').textContent).toBe(note)
    expect(screen.queryByTestId('content')).toBeNull()
    expect(screen.queryByText('Steered into the running turn')).toBeNull()
  })

  describe('settling into the note', () => {
    beforeEach(() => vi.mocked(revealComposer).mockClear())

    const retracted = (action: string) => ({ steer: true, steerState: 'retracted', steerRetractAction: action, mid: 'm-1' })

    it('collapses the bubble into the note inside the same row element', () => {
      const { container, rerender } = renderSteer(pending, { onSteerAction: vi.fn() })
      const row = container.querySelector('[data-role="user"]')

      // The server clears a retracted row's text, so the collapse draws the body
      // the row last showed.
      rerender(<UserMessage content="" meta={retracted('queue')} renderContent={renderContent} slotRunning onSteerAction={vi.fn()} />)

      expect(container.querySelector('[data-role="user"]')).toBe(row)
      expect(row?.getAttribute('data-steer-settle')).toBe('animated')
      expect(screen.getByTestId('steer-retract-collapse').textContent).toBe('use the blue fleet')
      expect(screen.getByTestId('steer-retract-collapse').getAttribute('aria-hidden')).toBe('true')
      expect(screen.getByTestId('steer-retracted').textContent).toBe('You moved this message to the queue')
    })

    it('draws a row read back already retracted as the note, with no collapse', () => {
      const { container } = render(<UserMessage content="" meta={retracted('edit')} renderContent={renderContent} slotRunning={false} />)

      expect(container.querySelector('[data-role="user"]')?.getAttribute('data-steer-settle')).toBe('static')
      expect(screen.queryByTestId('steer-retract-collapse')).toBeNull()
      expect(screen.getByTestId('steer-retracted').textContent).toBe('You moved this message back to your message box')
    })

    it.each(['edit', 'queue'])('makes the %s note a button that reveals the composer', action => {
      render(<UserMessage content="" meta={retracted(action)} renderContent={renderContent} />)

      const note = screen.getByTestId('steer-retracted')
      expect(note.tagName).toBe('BUTTON')
      expect(note.getAttribute('type')).toBe('button')
      expect(screen.getByRole('button', { name: note.textContent ?? '' })).toBe(note)
      fireEvent.click(note)
      expect(revealComposer).toHaveBeenCalledTimes(1)
    })

    it('keeps the Unsend note plain text, since the text went nowhere', () => {
      render(<UserMessage content="" meta={retracted('unsend')} renderContent={renderContent} />)

      const note = screen.getByTestId('steer-retracted')
      expect(note.tagName).not.toBe('BUTTON')
      expect(note.closest('button')).toBeNull()
      fireEvent.click(note)
      expect(revealComposer).not.toHaveBeenCalled()
    })
  })
})
