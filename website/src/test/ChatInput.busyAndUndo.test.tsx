// The rest of ChatInput's suite: focus (autoFocusKey and the global "/"
// shortcut), Quick Send, the stop / steer / queue button while a turn runs (with
// its steer-only form and split-menu hint), the Project chip, and undo/redo
// including paste tokens. ChatInput.test.tsx covers the composer's core; the two
// module mocks and the beforeEach below are copied from it word for word, and
// defaultProps lives in ChatInputTestUtils.tsx.
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { releaseComposerForKeyboardSwitch } from '../pages/chat/composerFocus'
import { safeSetItem } from '../utils/safeStorage'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import type { PasteBlock } from '../utils/pasteTokens'
import { defaultProps } from './ChatInputTestUtils'

// isTouchDevice gates the autoFocusKey effect (tapping a session must not pop
// the soft keyboard). Default false so the desktop cases behave as before; a
// touch-device case flips it on per-test.
const touchEnv = vi.hoisted(() => ({ touch: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

const mobileEnv = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobileEnv.mobile }))

beforeEach(() => {
  vi.restoreAllMocks()
  // After restoreAllMocks: it would otherwise undo the layout stub. jsdom does
  // no layout, and the composer MEASURES its strips, so a strip with no stubbed
  // box measures 0 and reads as no strip at all.
  stubStripHeights()
  localStorage.clear()
  touchEnv.touch = false
  mobileEnv.mobile = false
})

describe('ChatInput', () => {

  describe('autoFocusKey', () => {
    it('focuses textarea on first non-null key', () => {
      renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      expect(screen.getByLabelText('Message input')).toHaveFocus()
    })

    it('focuses textarea when key changes', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" />)
      expect(ta).toHaveFocus()
    })

    it('does not re-focus on a re-render with the same key', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" />)
      expect(ta).not.toHaveFocus()
    })

    it('skips exactly one autofocus after a keyboard-driven switch released the composer (macOS chord chaining)', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      // A keyboard jump armed the release: this switch's autofocus is
      // skipped so the next chord is not input-gated dead on macOS.
      releaseComposerForKeyboardSwitch()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" />)
      expect(ta).not.toHaveFocus()
      // One-shot: the next switch (pointer-driven — no release) focuses again.
      rerender(<ChatInput {...defaultProps} autoFocusKey="C" />)
      expect(ta).toHaveFocus()
    })

    it('does not focus on a touch device, even when the key changes', () => {
      // Tapping a session on a phone/tablet must not pop the soft keyboard.
      touchEnv.touch = true
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" />)
      expect(ta).not.toHaveFocus()
    })

    it('does not re-focus when disabled flips false on the same key (e.g. AI finishes responding)', () => {
      // Once a key has been focused, subsequent disabled flips for the SAME key
      // must not steal focus back from a user who is reading or scrolling.
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      expect(ta).toHaveFocus()
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" disabled />)
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" disabled={false} />)
      expect(ta).not.toHaveFocus()
    })

    it('defers focus when key changes while disabled, then applies it once disabled clears', () => {
      // User picks a different session while it is still stopping (disabled=true).
      // Focus must NOT be lost — it should land once disabled flips false.
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" disabled />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" disabled />)
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" disabled={false} />)
      expect(ta).toHaveFocus()
    })

    it('does not steal focus from another input element', () => {
      const { rerender } = renderWithProviders(
        <>
          <input data-testid="other" aria-label="Other input" />
          <ChatInput {...defaultProps} autoFocusKey="A" />
        </>,
      )
      const other = screen.getByTestId('other')
      other.focus()
      expect(other).toHaveFocus()
      rerender(
        <>
          <input data-testid="other" aria-label="Other input" />
          <ChatInput {...defaultProps} autoFocusKey="B" />
        </>,
      )
      expect(other).toHaveFocus()
    })
  })

  describe('Quick Send', () => {
    it('passes quickSend to FollowUpBar when options present', () => {
      renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['A', 'B']} followUpPicked={new Set()} onFollowUpSelect={vi.fn()} quickSend={true} />)
      // The instant-send hint now lives in the hover tooltip, not a title attribute.
      fireEvent.focus(screen.getByRole('button', { name: 'A' }))
      expect(screen.getByRole('tooltip').textContent).toMatch(/Click to send instantly/)
      fireEvent.blur(screen.getByRole('button', { name: 'A' }))
    })

    it('fires onFollowUpSelect with MouseEvent on option click', () => {
      const onSelect = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['Go']} followUpPicked={new Set()} onFollowUpSelect={onSelect} quickSend={true} />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }))
      // Fourth arg: the composer's chip send, which the host's quick-send uses.
      expect(onSelect).toHaveBeenCalledWith('Go', expect.any(Object), undefined, expect.any(Function))
    })
  })

  describe('stop button', () => {
    it('shows armed Stop button while running, click calls onStop', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={onStop} />)
      const btn = screen.getByTestId('stop-button-armed')
      expect(btn).toHaveAttribute('aria-label', 'Stop generation')
      fireEvent.click(btn)
      expect(onStop).toHaveBeenCalled()
    })

    it('shows pulsing force-kill button when soft_pending, click calls onStop', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={onStop} stopState="soft_pending" />)
      const btn = screen.getByTestId('stop-button-pulsing')
      expect(btn).toHaveAttribute('aria-label', 'Force stop (your messages stay; in-progress work and queued messages are discarded)')
      fireEvent.click(btn)
      expect(onStop).toHaveBeenCalled()
    })

    it('keeps showing the stop affordance when soft_pending even after isRunning flips false', () => {
      // Regression: the chat_done event clears isRunning before the backend
      // flips stop_state back to idle. The button must NOT revert to Send mid-stop.
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning={false} onStop={onStop} stopState="soft_pending" />)
      expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument()
    })

    it('shows disabled killing spinner when stopState is killing (after isRunning false)', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning={false} onStop={onStop} stopState="killing" />)
      const btn = screen.getByRole('button', { name: 'Force stopping session' })
      expect(btn).toBeDisabled()
      fireEvent.click(btn)
      expect(onStop).not.toHaveBeenCalled()
    })

    it('shows Queue message button (calls onSend) when running with pending text', () => {
      const onSend = vi.fn()
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="more" isRunning onStop={onStop} onSend={onSend} />)
      const btn = screen.getByRole('button', { name: 'Queue message' })
      fireEvent.click(btn)
      expect(onSend).toHaveBeenCalled()
      expect(onStop).not.toHaveBeenCalled()
    })
  })

  describe('Optimize prompt while a turn runs (issue #23572)', () => {
    // Optimize rewrites the draft and never sends, so a running turn must not
    // take it away: a steer or queued prompt is written mid-turn. The busy send
    // cluster is at its button cap, so mid-turn it is a row in the "+" menu.
    const busy = { ...defaultProps, value: 'tighten this', isRunning: true, onStop: vi.fn(), onUploadFiles: vi.fn() }
    const openPlusMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))
    const optimizeRow = () => screen.queryByTestId('composer-menu-optimize')

    it('offers Optimize in the + menu while a draft would queue, and adds no button to the send cluster', () => {
      renderWithProviders(<ChatInput {...busy} />)
      expect(screen.getByRole('button', { name: 'Queue message' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Optimize prompt' })).not.toBeInTheDocument()
      openPlusMenu()
      expect(optimizeRow()).toHaveTextContent('Optimize prompt')
      expect(optimizeRow()).not.toBeDisabled()
    })

    it('offers it beside the steer/queue split button too', () => {
      renderWithProviders(<ChatInput {...busy} canSteer onSteer={vi.fn()} />)
      expect(screen.getByTestId('busy-send-button')).toBeInTheDocument()
      openPlusMenu()
      expect(optimizeRow()).not.toBeDisabled()
    })

    it('disables the row on an empty composer, as the idle button is', () => {
      renderWithProviders(<ChatInput {...busy} value="" />)
      expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
      openPlusMenu()
      expect(optimizeRow()).toBeDisabled()
    })

    it('has no row when idle (the cluster button is back) or when the host opted out', () => {
      const { unmount } = renderWithProviders(<ChatInput {...busy} isRunning={false} />)
      expect(screen.getByRole('button', { name: 'Optimize prompt' })).toBeInTheDocument()
      openPlusMenu()
      expect(optimizeRow()).not.toBeInTheDocument()
      unmount()
      renderWithProviders(<ChatInput {...busy} promptOptimizer={false} />)
      openPlusMenu()
      expect(optimizeRow()).not.toBeInTheDocument()
    })

    it('offers it in the touch overflow, where + opens the file picker directly', () => {
      touchEnv.touch = true
      renderWithProviders(<ChatInput {...busy} collapsible />)
      fireEvent.keyDown(screen.getByTestId('composer-more-trigger'), { key: 'Enter' })
      expect(screen.getByTestId('composer-more-optimize')).toHaveTextContent('Optimize prompt')
    })

    it('starts the optimize from the row, and holds the queue button while it rewrites the draft', async () => {
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() => new Promise<Response>(() => {}))
      try {
        const onSend = vi.fn()
        renderWithProviders(<ChatInput {...busy} onSend={onSend} />)
        openPlusMenu()
        fireEvent.click(optimizeRow()!)
        await waitFor(() => expect(fetchSpy.mock.calls.some(c => String(c[0]).includes('optimize'))).toBe(true))
        expect(optimizeRow()).not.toBeInTheDocument() // the menu closed
        const queue = screen.getByRole('button', { name: 'Queue message' })
        await waitFor(() => expect(queue).toBeDisabled())
        fireEvent.click(queue)
        expect(onSend).not.toHaveBeenCalled()
      } finally {
        fetchSpy.mockRestore()
      }
    })
  })

  describe('a staged session reference does not arm the mid-turn button', () => {
    // Regression guard for a dead click this feature briefly introduced. A
    // staged reference correctly enables the IDLE send button, but the mid-turn
    // split button must stay out of it: its steer mode refuses a payload of refs
    // alone, so enabling it produced an enabled primary button whose press did
    // nothing. Before session refs existed, an empty composer mid-turn rendered
    // the stop button — that is the behaviour to preserve.
    const refProps = (overrides: Record<string, unknown> = {}) => ({
      ...defaultProps,
      value: '',
      pendingSessions: [{ key: 'chat-9', title: 'Release notes', messages: 12 }],
      isRunning: true,
      canSteer: true,
      onStop: vi.fn(),
      onSend: vi.fn(),
      onSteer: vi.fn(),
      ...overrides,
    })

    it('renders the stop button, not the split send button, with only a ref staged', () => {
      renderWithProviders(<ChatInput {...refProps()} />)
      expect(screen.queryByTestId('busy-send-button')).not.toBeInTheDocument()
      expect(screen.queryByTestId('busy-send-caret')).not.toBeInTheDocument()
    })

    it('still shows the chip so the reference is visibly staged, not lost', () => {
      renderWithProviders(<ChatInput {...refProps()} />)
      expect(screen.getByTestId('session-ref-chip')).toHaveAttribute('data-session-ref', 'chat-9')
    })

    it('arms the mid-turn button again as soon as there is real text to steer', () => {
      renderWithProviders(<ChatInput {...refProps({ value: 'also look at this' })} />)
      expect(screen.getByTestId('busy-send-button')).toBeInTheDocument()
    })

    it('does enable the IDLE send button with only a ref staged', () => {
      const p = refProps({ isRunning: false, canSteer: false })
      renderWithProviders(<ChatInput {...p} />)
      const send = screen.getByLabelText('Send')
      expect(send).not.toBeDisabled()
      fireEvent.click(send)
      expect(p.onSend).toHaveBeenCalledTimes(1)
    })
  })

  describe('split send button while running (steer default)', () => {
    const runningProps = (overrides: Record<string, unknown> = {}) => ({
      ...defaultProps,
      value: 'more',
      isRunning: true,
      canSteer: true,
      onStop: vi.fn(),
      onSend: vi.fn(),
      onSteer: vi.fn() as (() => void) | undefined,
      ...overrides,
    })

    it('renders Steer as the default main action with a dropdown caret', () => {
      renderWithProviders(<ChatInput {...runningProps()} />)
      expect(screen.getByTestId('busy-send-button')).toHaveAttribute('aria-label', 'Steer')
      expect(screen.getByTestId('busy-send-caret')).toBeInTheDocument()
    })

    it('main button fires onSteer (not onSend) in steer mode', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-button'))
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('Enter steers while running in steer mode', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    // ⌘↩ / Ctrl+Enter while the split is showing performs the OTHER action for
    // that one send (#4608, the Claude Code / Codex gesture).
    it('Ctrl+Enter queues (onSend) while running in steer mode — the one-off flip', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', ctrlKey: true })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
      // The flip is one-shot: the next plain Enter is back to the split's mode.
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSteer).toHaveBeenCalledTimes(1)
    })

    it('Ctrl+Enter steers while running in queue mode', () => {
      safeSetItem('mc-busy-send-mode:no-slot', 'queue')
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', metaKey: true })
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('Ctrl+Enter is a plain send when the composer is idle (unchanged behaviour)', () => {
      const p = runningProps({ isRunning: false })
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', ctrlKey: true })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
    })

    it('Ctrl+Enter cannot steer a slot with no steer path — it queues like Enter', () => {
      const p = runningProps({ canSteer: false, onSteer: undefined })
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', ctrlKey: true })
      expect(p.onSend).toHaveBeenCalledTimes(1)
    })

    it('in ctrl-enter send mode the modified Enter is the send key, not a flip', () => {
      const p = runningProps({ sendOnEnter: 'ctrl-enter' as const })
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', ctrlKey: true })
      expect(p.onSteer).toHaveBeenCalledTimes(1) // split mode (steer), no flip
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('clicking the split main button never flips (a MouseEvent is not `true`)', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-button'))
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('the split menu names the flip chord', () => {
      renderWithProviders(<ChatInput {...runningProps()} />)
      fireEvent.click(screen.getByTestId('busy-send-caret'))
      expect(screen.getByText(/queues this message instead/)).toBeInTheDocument()
    })

    it('dropdown switches to Queue — main button and Enter then queue, choice persists', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-caret'))
      fireEvent.click(screen.getByTestId('busy-send-mode-queue'))
      // The test store has no active slot, so the write lands on the slot-less
      // sentinel key; the unscoped legacy key is a read-only migration source.
      expect(localStorage.getItem('mc-busy-send-mode:no-slot')).toBe('queue')
      const main = screen.getByTestId('busy-send-button')
      expect(main).toHaveAttribute('aria-label', 'Queue message')
      fireEvent.click(main)
      expect(p.onSend).toHaveBeenCalledTimes(1)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(2)
      expect(p.onSteer).not.toHaveBeenCalled()
    })

    it('restores persisted queue mode from localStorage', () => {
      safeSetItem('mc-busy-send-mode', 'queue')
      renderWithProviders(<ChatInput {...runningProps()} />)
      expect(screen.getByTestId('busy-send-button')).toHaveAttribute('aria-label', 'Queue message')
    })

    it('dropdown is keyboard operable: focus on open, arrows roam, Escape returns to caret', async () => {
      renderWithProviders(<ChatInput {...runningProps()} />)
      fireEvent.click(screen.getByTestId('busy-send-caret'))
      // useListboxKeyboard focuses the first option on open (setTimeout 0)
      await waitFor(() => expect(screen.getByTestId('busy-send-mode-steer')).toHaveFocus())
      const menu = screen.getByRole('menu')
      fireEvent.keyDown(menu, { key: 'ArrowDown' })
      expect(screen.getByTestId('busy-send-mode-queue')).toHaveFocus()
      fireEvent.keyDown(menu, { key: 'ArrowUp' })
      expect(screen.getByTestId('busy-send-mode-steer')).toHaveFocus()
      fireEvent.keyDown(menu, { key: 'Escape' })
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      expect(screen.getByTestId('busy-send-caret')).toHaveFocus()
    })

    it('shows split button with only pending files (image-only steer)', () => {
      const p = { ...runningProps(), value: '', pendingFiles: ['/tmp/shot.png'] }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-button'))
      expect(p.onSteer).toHaveBeenCalledTimes(1)
    })

    it('Enter falls back to queue while stopping (soft_pending) even in steer mode', () => {
      const p = { ...runningProps(), stopState: 'soft_pending' as const }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
    })

    it('Enter sends normally when not running even if onSteer is provided', () => {
      const p = { ...runningProps(), isRunning: false, onStop: undefined }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
    })
  })

  /* busyMode="steer-only": the surface has no queue concept (a member DM
   * thread is a conversation with one named peer). While busy the composer
   * keeps the PLAIN send button and every send steers. The main chat and
   * split view never pass it, so the default stays the split button.
   * Mutation checks: drop `steerOnly ||` from steerActive -> the persisted-
   * queue test goes RED; drop the steer-only render branch -> the "no
   * split" tests go RED. */
  describe('busyMode="steer-only" while running', () => {
    const steerOnlyProps = () => ({
      ...defaultProps,
      value: 'more',
      isRunning: true,
      canSteer: true,
      busyMode: 'steer-only' as const,
      onStop: vi.fn(),
      onSend: vi.fn(),
      onSteer: vi.fn(),
    })

    it('renders the plain send button — no split button, no caret, no mode picker', () => {
      renderWithProviders(<ChatInput {...steerOnlyProps()} />)
      const send = screen.getByTestId('steer-only-send')
      expect(send).toHaveAttribute('aria-label', 'Send')
      expect(send).not.toBeDisabled()
      expect(screen.queryByTestId('busy-send-button')).not.toBeInTheDocument()
      expect(screen.queryByTestId('busy-send-caret')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Queue message' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Send options' })).not.toBeInTheDocument()
    })

    it('click steers (onSteer), never onSend', () => {
      const p = steerOnlyProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('steer-only-send'))
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('Enter steers', () => {
      const p = steerOnlyProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('ignores a persisted Queue preference — there is no Queue on this surface', () => {
      safeSetItem('mc-busy-send-mode', 'queue')
      const p = steerOnlyProps()
      renderWithProviders(<ChatInput {...p} />)
      expect(screen.getByTestId('steer-only-send')).toHaveAttribute('aria-label', 'Send')
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('still falls back to onSend while stopping (soft_pending) — stop state wins', () => {
      const p = { ...steerOnlyProps(), stopState: 'soft_pending' as const }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
    })

    it('idle composer sends normally (onSend) regardless of busyMode', () => {
      const p = { ...steerOnlyProps(), isRunning: false }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByLabelText('Send'))
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
      expect(screen.queryByTestId('steer-only-send')).not.toBeInTheDocument()
    })

    it('without a steer path it degrades to the queue button like the split mode does', () => {
      const p = { ...steerOnlyProps(), canSteer: false, onSteer: undefined }
      renderWithProviders(<ChatInput {...p} />)
      expect(screen.queryByTestId('steer-only-send')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Queue message' })).toBeInTheDocument()
    })

    it('the default busyMode is the split button (main chat unchanged)', () => {
      // An explicit `undefined` resolves to the prop default exactly as an
      // omitted prop does — this is what ChatPage and split view pass.
      renderWithProviders(<ChatInput {...steerOnlyProps()} busyMode={undefined} />)
      expect(screen.getByTestId('busy-send-button')).toBeInTheDocument()
      expect(screen.getByTestId('busy-send-caret')).toBeInTheDocument()
      expect(screen.queryByTestId('steer-only-send')).not.toBeInTheDocument()
    })
  })

  describe('global "/" focus shortcut', () => {
    it('focuses textarea when "/" is pressed outside any input', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      const ta = screen.getByLabelText('Message input')
      ;(ta as HTMLElement).blur()
      fireEvent.keyDown(document, { key: '/' })
      expect(document.activeElement).toBe(ta)
    })

    it('does not intercept "/" when already typing in the textarea', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      const ta = screen.getByLabelText('Message input')
      ta.focus()
      fireEvent.keyDown(ta, { key: '/' })
      expect(document.activeElement).toBe(ta)
    })
  })

  describe('Project chip', () => {
    it('does NOT render the project chip when onProjectClick is undefined', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.queryByLabelText(/Project:/)).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Select project')).not.toBeInTheDocument()
    })

    it('renders "Project" label when project prop is empty', () => {
      renderWithProviders(<ChatInput {...defaultProps} onProjectClick={vi.fn()} />)
      const btn = screen.getByLabelText('Select project')
      expect(btn).toBeInTheDocument()
      expect(btn.getAttribute('title')).toBe('Select project')
      expect(btn.textContent).toContain('Project')
    })

    it('renders the project basename as label when project is set', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={vi.fn()} project="/home/u/workplace/KiroCrew" />
      )
      const btn = screen.getByLabelText('Project: /home/u/workplace/KiroCrew')
      expect(btn).toBeInTheDocument()
      expect(btn.getAttribute('title')).toBe('Project: /home/u/workplace/KiroCrew')
      expect(btn.textContent).toContain('KiroCrew') // brand-ok: KiroCrew is the fixture folder name
      expect(btn.textContent).not.toContain('/home/u/workplace')
    })

    it('falls back to full path label when project has no basename', () => {
      // project = "/" → split('/').filter(Boolean) = [] → pop() = undefined → falls back to project itself
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={vi.fn()} project="/" />
      )
      const btn = screen.getByLabelText('Project: /')
      expect(btn.textContent).toContain('/')
    })

    it('strips trailing slashes when computing the basename label', () => {
      // project = "/home/u/foo/" → filter(Boolean) drops empty trailing chunk → 'foo'
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={vi.fn()} project="/home/u/foo/" />
      )
      const btn = screen.getByLabelText('Project: /home/u/foo/')
      expect(btn.textContent).toContain('foo')
    })

    it('calls onProjectClick with the chip\'s bounding rect when clicked', () => {
      const onProjectClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={onProjectClick} project="/home/u/proj" />
      )
      const btn = screen.getByLabelText('Project: /home/u/proj')
      fireEvent.click(btn)
      expect(onProjectClick).toHaveBeenCalledTimes(1)
      const arg = onProjectClick.mock.calls[0][0]
      // jsdom returns a 0,0,0,0 rect but it MUST be a DOMRect-shaped object
      expect(arg).toBeDefined()
      expect(typeof arg.top).toBe('number')
      expect(typeof arg.left).toBe('number')
      expect(typeof arg.right).toBe('number')
      expect(typeof arg.bottom).toBe('number')
      expect(typeof arg.width).toBe('number')
      expect(typeof arg.height).toBe('number')
    })

    it('still calls onProjectClick when no project is set (chip in placeholder mode)', () => {
      const onProjectClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={onProjectClick} />
      )
      fireEvent.click(screen.getByLabelText('Select project'))
      expect(onProjectClick).toHaveBeenCalledTimes(1)
    })
  })
})

// --- Prompt undo/redo ---
// A controlled harness so onChange -> value round-trips exactly like ChatPage,
// which is what makes the explicit undo history observable end-to-end.
function ControlledInput({ initial = '', extra = {} }: { initial?: string; extra?: Record<string, unknown> }) {
  const [v, setV] = React.useState(initial)
  return <ChatInput {...defaultProps} value={v} onChange={setV} {...extra} />
}

describe('ChatInput undo/redo', () => {
  const undo = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
  const redoShift = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true, shiftKey: true })

  it('restores text with Ctrl+Z after an accidental full erase', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello world' } })
    fireEvent.change(ta, { target: { value: '' } }) // select-all + delete
    expect(ta.value).toBe('')
    undo(ta)
    expect(ta.value).toBe('hello world')
  })

  it('supports Cmd+Z on macOS (metaKey)', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'draft text here' } })
    fireEvent.change(ta, { target: { value: '' } })
    fireEvent.keyDown(ta, { key: 'z', metaKey: true })
    expect(ta.value).toBe('draft text here')
  })

  it('Ctrl+Shift+Z redoes an undone change', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'redo me please' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('redo me please')
    redoShift(ta)
    expect(ta.value).toBe('')
  })

  it('Ctrl+Y redoes (Windows convention)', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'windows redo' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('windows redo')
    fireEvent.keyDown(ta, { key: 'y', ctrlKey: true })
    expect(ta.value).toBe('')
  })

  it('collapses a rapid typing burst into a single undo step', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    // Incremental keystrokes within the coalesce window merge into one entry.
    for (const v of ['a', 'ab', 'abc', 'abcd']) fireEvent.change(ta, { target: { value: v } })
    undo(ta)
    expect(ta.value).toBe('') // one undo clears the whole burst
  })

  it('is a no-op at the base of the history (nothing to undo)', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    undo(ta)
    expect(ta.value).toBe('')
  })

  it('does not undo while an IME composition is active', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'こんにちは' } })
    fireEvent.compositionStart(ta)
    fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
    expect(ta.value).toBe('こんにちは') // composition guard suppressed undo
  })

  it('discards the redo branch after a new edit following undo', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'first version' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('first version')
    fireEvent.change(ta, { target: { value: 'different text' } }) // drops redo branch
    redoShift(ta)
    expect(ta.value).toBe('different text') // redo is now a no-op
  })

  it('reseeds across a deferred slot draft restore (no cross-slot bleed)', () => {
    const drafts: Record<string, string> = { 'slot-a': 'slot A draft', 'slot-b': '' }
    function Lagged() {
      const [afk, setAfk] = React.useState('slot-a')
      const [v, setV] = React.useState(drafts['slot-a'])
      // Mimic ChatPage: activeSlot changes first; the draft is restored in a
      // SEPARATE commit by an [activeSlot]-keyed effect — so `value` lags
      // `autoFocusKey` by one commit.
      React.useEffect(() => { setV(drafts[afk]) }, [afk])
      return (
        <>
          <button onClick={() => setAfk('slot-b')}>switch</button>
          <ChatInput {...defaultProps} value={v} onChange={setV} autoFocusKey={afk} />
        </>
      )
    }
    renderWithProviders(<Lagged />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toBe('slot A draft')
    fireEvent.click(screen.getByText('switch'))
    expect(ta.value).toBe('') // slot B's draft settled in the second commit
    undo(ta)
    expect(ta.value).toBe('') // MUST NOT restore slot A's draft
  })

  it('reseeds synchronously when the draft lands in the same commit as the switch', () => {
    // if ChatPage ever restores the draft in the SAME commit as the
    // slot switch (no lag), the first keystroke must not be folded into the undo
    // base — Ctrl+Z must still reach the restored draft.
    const drafts: Record<string, string> = { 'slot-a': 'alpha draft', 'slot-b': 'beta draft' }
    function SyncSwitch() {
      const [afk, setAfk] = React.useState('slot-a')
      const [v, setV] = React.useState(drafts['slot-a'])
      // Both activeSlot and the draft change in one commit (synchronous restore).
      const go = () => { setAfk('slot-b'); setV(drafts['slot-b']) }
      return (
        <>
          <button onClick={go}>switch</button>
          <ChatInput {...defaultProps} value={v} onChange={setV} autoFocusKey={afk} />
        </>
      )
    }
    renderWithProviders(<SyncSwitch />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toBe('alpha draft')
    fireEvent.click(screen.getByText('switch'))
    expect(ta.value).toBe('beta draft')
    fireEvent.change(ta, { target: { value: 'beta draft!' } }) // user's first keystroke (real DOM edit)
    undo(ta)
    expect(ta.value).toBe('beta draft') // first edit is undoable back to the restored draft (no fold)
  })

  it('starts a new undo step after a pause longer than the coalesce window', () => {
    vi.useFakeTimers()
    try {
      renderWithProviders(<ControlledInput />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.change(ta, { target: { value: 'aa' } })
      vi.advanceTimersByTime(450) // past UNDO_COALESCE_MS (400)
      fireEvent.change(ta, { target: { value: 'aabb' } })
      undo(ta)
      expect(ta.value).toBe('aa') // the pause made 'aabb' its own undo step
    } finally {
      vi.useRealTimers()
    }
  })
})

// --- Undo restores deleted/expanded paste content ---
// A `[ Paste #N ]` token in the text is just a pointer; its content lives in a
// separate PasteBlock the parent owns. Deleting or expanding a token drops the
// block, so the undo snapshot must carry the blocks to make the paste
// recoverable — restoring only the text would resurrect a dead token literal.
describe('ChatInput undo/redo: paste content', () => {
  const undo = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
  const redoShift = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true, shiftKey: true })

  // Harness mirroring ChatPage: value and pasteBlocks are separate controlled
  // state, wired to the two callbacks the way the real parent wires them.
  function PasteHarness({ initial, initialBlocks }: { initial: string; initialBlocks: PasteBlock[] }) {
    const [v, setV] = React.useState(initial)
    const [blocks, setBlocks] = React.useState<PasteBlock[]>(initialBlocks)
    return (
      <ChatInput
        {...defaultProps}
        value={v}
        onChange={setV}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
      />
    )
  }

  const block: PasteBlock = { id: 'p1', seq: 1, lines: 40, content: 'TRACEBACK: boom\n...40 lines...' }
  const token = '[ Paste #1 · 40 lines ]'

  it('recovers the paste content after a Backspace-delete of the token', () => {
    // Caret just past the token; Backspace removes the token AND its block.
    renderWithProviders(<PasteHarness initial={`${token}`} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(token.length, token.length)
    fireEvent.keyDown(ta, { key: 'Backspace' })
    expect(ta.value).toBe('') // token gone

    undo(ta)
    expect(ta.value).toBe(token) // token text back...
    // ...and its content is recoverable: copy of a full selection expands it.
    ta.setSelectionRange(0, ta.value.length)
    const clip = { data: '' as string, setData: (_t: string, d: string) => { clip.data = d } }
    fireEvent.copy(ta, { clipboardData: { setData: clip.setData } })
    expect(clip.data).toBe(block.content) // block was restored, not a dead literal
  })

  it('recovers the paste after expanding the token, then undo re-collapses it', () => {
    // Double-click expands the token to full content and drops the block.
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(2, 2) // caret inside the token
    fireEvent.click(ta, { detail: 2 })
    expect(ta.value).toBe(block.content) // expanded inline

    undo(ta)
    expect(ta.value).toBe(token) // collapsed token restored
    ta.setSelectionRange(0, ta.value.length)
    const clip = { data: '' as string, setData: (_t: string, d: string) => { clip.data = d } }
    fireEvent.copy(ta, { clipboardData: { setData: clip.setData } })
    expect(clip.data).toBe(block.content)
  })

  it('redo re-drops the block so an undone delete can be redone', () => {
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(token.length, token.length)
    fireEvent.keyDown(ta, { key: 'Backspace' })
    expect(ta.value).toBe('')
    undo(ta)
    expect(ta.value).toBe(token)
    redoShift(ta)
    expect(ta.value).toBe('') // deletion reinstated
  })

  it('leaves plain-text undo untouched when no pastes exist', () => {
    renderWithProviders(<PasteHarness initial="" initialBlocks={[]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello world' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('hello world')
  })
})

// Expanding a collapsed-paste token in the composer differs by pointer class:
// mouse needs a double-click (select-then-expand); touch expands on one tap,
// because two discrete taps never coalesce into a detail>=2 click, so the
// double-click path is unreachable under a finger.
describe('ChatInput composer paste-token expand', () => {
  function PasteHarness({ initial, initialBlocks }: { initial: string; initialBlocks: PasteBlock[] }) {
    const [v, setV] = React.useState(initial)
    const [blocks, setBlocks] = React.useState<PasteBlock[]>(initialBlocks)
    return (
      <ChatInput
        {...defaultProps}
        value={v}
        onChange={setV}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
      />
    )
  }

  const block: PasteBlock = { id: 'p1', seq: 1, lines: 40, content: 'TRACEBACK: boom\n...40 lines...' }
  const token = '[ Paste #1 · 40 lines ]'

  it('expands the token on a single tap on a touch device (detail=1)', () => {
    touchEnv.touch = true
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(2, 2) // caret inside the token
    fireEvent.click(ta, { detail: 1 }) // a tap is a single click, never detail>=2
    expect(ta.value).toBe(block.content) // expanded inline on one tap
  })

  it('does NOT expand on a single mouse click (detail=1) — mouse selects first', () => {
    touchEnv.touch = false
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(2, 2)
    fireEvent.click(ta, { detail: 1 })
    expect(ta.value).toBe(token) // still collapsed — a second click is needed
  })

  it('expands on a mouse double-click (detail=2)', () => {
    touchEnv.touch = false
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(2, 2)
    fireEvent.click(ta, { detail: 2 })
    expect(ta.value).toBe(block.content) // expanded inline
  })
})

describe('ChatInput — busy split menu hint is mode-gated', () => {
  const props = (overrides: Record<string, unknown> = {}) => ({
    value: 'more', onChange: vi.fn(), isRunning: true, canSteer: true, onStop: vi.fn(), onSend: vi.fn(), onSteer: vi.fn(), ...overrides,
  })
  it('names the concrete flip for the current mode in `enter` send mode', () => {
    safeSetItem('mc-busy-send-mode:no-slot', 'queue')
    renderWithProviders(<ChatInput {...props()} />)
    fireEvent.click(screen.getByTestId('busy-send-caret'))
    expect(screen.getByText(/steers with this message instead/)).toBeInTheDocument()
  })
  it('shows no flip hint in ctrl-enter mode, where the chord is the send key', () => {
    renderWithProviders(<ChatInput {...props({ sendOnEnter: 'ctrl-enter' })} />)
    fireEvent.click(screen.getByTestId('busy-send-caret'))
    expect(screen.queryByText(/this message instead/)).not.toBeInTheDocument()
  })
})
