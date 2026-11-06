// The first production host of the close chip: the marker reaches the bar through
// `deriveFollowUpOptions` and `ChatInput`, and a click closes the slot through the store.
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, act, renderHook } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { defaultProps } from './ChatInputTestUtils'
import { deriveFollowUpOptions } from '../app-sdk/protocol/options'
import { optionActionOf, stripOptionActionTag } from '../app-sdk/protocol/optionActionTag'
import { closeFromActionChip, slotUnsentWorkSource } from '../pages/chat/page/actionChipClose'
import { api } from '../api/client'
import { __resetForTests as resetSideDrafts, clearSideDraft, writeSideDraft } from '../utils/sideComposerDrafts'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { resetRecentlyClosedSessions, takeRecentlyClosedSession } from '../lib/recentlyClosedSessions'
import type { ChatSlot } from '../types'
import { __resetSlotDirtyForTests } from '../utils/slotDirtyBeacon'
import { __resetClosingIntentForTests, __setClosingAckWindowForTests, publishClosingIntent, closingIntentVetoed, CLOSING_KEY_PREFIX } from '../utils/slotClosingIntent'
import { anyWindowSlotDirty } from '../utils/slotDirtyBeacon'
import { useSlotWorkBeacon, localComposerHoldsWork, __resetSlotWorkBeaconForTests } from '../hooks/useSlotWorkBeacon'
import { closeConfirmPrompt } from '../lib/closeConfirmPrompt'
import { askAppConfirm } from '../lib/appConfirm'
import type { ChatMessage } from '../types'
import type { OptionAction } from '../components/FollowUpBar'

vi.mock('../lib/closeConfirmPrompt', () => ({ closeConfirmPrompt: vi.fn(() => null) }))
vi.mock('../lib/appConfirm', () => ({ askAppConfirm: vi.fn(async () => true) }))

const assistant = (content: string, ts = '1'): ChatMessage =>
  ({ role: 'assistant', content, ts }) as unknown as ChatMessage
const CLOSE: OptionAction = { action: 'close', label: 'All done' }
const actionChip = () => document.querySelector('[data-option-action]') as HTMLElement

beforeEach(() => {
  localStorage.clear()
  resetSideDrafts()
  __resetSlotDirtyForTests()
  __resetClosingIntentForTests()
  __setClosingAckWindowForTests(0)
  __resetSlotWorkBeaconForTests()
  vi.mocked(closeConfirmPrompt).mockReturnValue(null)
  vi.mocked(askAppConfirm).mockResolvedValue(true)
})
afterEach(() => vi.restoreAllMocks())

const tag = (body: string) => `<!-- option-action:${body} -->`



describe('optionActionOf reads the control tag at the reply tail', () => {
  it('offers close with its label', () => {
    expect(optionActionOf(`Done.\n${tag('close=All done')}`)).toEqual(CLOSE)
  })

  it('reads the head in any case, and after the [OPTIONS:] line', () => {
    expect(optionActionOf(`Done.\n[OPTIONS: A | B]\n<!-- OPTION-ACTION:close=All done -->`)).toEqual(CLOSE)
  })

  it('reads it from a stacked control-tag block', () => {
    expect(optionActionOf(`Done.\n${tag('close=All done')}\n<!-- deliver:slack -->\n`)).toEqual(CLOSE)
  })

  it('offers nothing for an unknown action, a missing =, or an empty label', () => {
    for (const body of ['open=All done', 'close', 'close=  ']) {
      expect(optionActionOf(`Done.\n${tag(body)}`), body).toBeNull()
    }
  })

  it('offers nothing when prose follows the tag, or the tag is mid-reply', () => {
    expect(optionActionOf(`${tag('close=Done')}\nMore words.`)).toBeNull()
    expect(optionActionOf(`Say ${tag('close=Done')}`)).toBeNull()
  })

  it('offers nothing past the 256-character body bound', () => {
    expect(optionActionOf(`Done.\n${tag(`close=${'x'.repeat(251)}`)}`)).toBeNull()
    expect(optionActionOf(`Done.\n${tag(`close=${'x'.repeat(200)}`)}`)).not.toBeNull()
  })

  it.each([
    ['a tab-indented line', `Example:\n\t${tag('close=Done')}`],
    ['a fence nested in a list item', `- ~~~html\n  ${tag('close=Done')}`],
    ['a fence in a blockquote', `> \`\`\`\n> x\n${tag('close=Done')}`],
    ['a fence indented four spaces', `    ~~~\n${tag('close=Done')}`],
  ])('offers nothing and strips nothing when the tag may be literal code — %s', (_name, raw) => {
    expect(optionActionOf(raw)).toBeNull()
    expect(stripOptionActionTag(raw)).toBe(raw)
  })

  it('CONTROL: list items without a fence leave a later tag live', () => {
    expect(optionActionOf(`- one\n- two\n${tag('close=Done')}`)).toEqual({ action: 'close', label: 'Done' })
  })

  it('offers nothing for an indented-code line', () => {
    expect(optionActionOf(`Done.\n    ${tag('close=Done')}`)).toBeNull()
  })

  it.each([
    ['an unclosed backtick fence', 'Like this:\n```\n'],
    ['an unclosed tilde fence', 'Like this:\n~~~\n'],
  ])('offers nothing from inside %s', (_name, before) => {
    const raw = `${before}${tag('close=Done')}`
    expect(optionActionOf(raw)).toBeNull()
    expect(stripOptionActionTag(raw)).toBe(raw)
  })

  it('CONTROL: a fence closed earlier leaves a later tag live', () => {
    expect(optionActionOf(`~~~\nx\n~~~\nDone.\n${tag('close=Done')}`)).toEqual({ action: 'close', label: 'Done' })
  })

  // The backend's control-tag strip gives these same outputs (the `option-action` rows of
  // test/fixtures/control_tag_corpus.json `backend_stripped`), so web copy and search agree with it.
  it.each([
    ['alone', 'all done\n<!-- option-action:close=Done -->', 'all done'],
    ['uppercase head', 'all done\n<!-- OPTION-ACTION:close=Done -->', 'all done'],
    ['after an [OPTIONS:] line', 'Pick one.\n[OPTIONS: Yes | No]\n<!-- option-action:close=Done -->', 'Pick one.\n[OPTIONS: Yes | No]'],
    ['inside an unterminated fence', '```\n<!-- option-action:close=Done -->', '```\n<!-- option-action:close=Done -->'],
    ['a label carrying >', 'all done\n<!-- option-action:close=a>b -->', 'all done\n<!-- option-action:close=a>b -->'],
  ])('web copy and search drop a standalone tag as the backend does — %s', (_name, input, stripped) => {
    expect(stripOptionActionTag(input)).toBe(stripped)
  })

  it('strips only the live tag line', () => {
    expect(stripOptionActionTag(`Done.\n${tag('close=Done')}`)).toBe('Done.')
    expect(stripOptionActionTag('Done.')).toBe('Done.')
  })
})

describe('deriveFollowUpOptions carries the action', () => {
  it('offers the action beside the options from the same row', () => {
    const d = deriveFollowUpOptions([assistant(`Done.\n[OPTIONS: A | B]\n${tag('close=All done')}`)], false)
    expect(d.followUpOptions).toEqual(['A', 'B'])
    expect(d.followUpAction).toEqual(CLOSE)
  })

  it('offers an action-only row, with no content options', () => {
    const d = deriveFollowUpOptions([assistant(`Done.\n${tag('close=All done')}`, '7')], false)
    expect(d.followUpOptions).toEqual([])
    expect(d.followUpAction).toEqual(CLOSE)
    expect(d.followUpSourceKey).toBe('7')
  })

  it('offers nothing once the user has answered', () => {
    const user = { role: 'user', content: 'thanks', ts: '2' } as unknown as ChatMessage
    expect(deriveFollowUpOptions([assistant(`Done.\n${tag('close=All done')}`), user], false).followUpAction).toBeNull()
  })
})

describe('ChatInput renders the chip from the host props', () => {
  it('renders an action-only bar and runs the action on click', () => {
    const onFollowUpAction = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} followUpOptions={[]} onFollowUpSelect={vi.fn()} followUpSourceKey="7"
        followUpAction={CLOSE} onFollowUpAction={onFollowUpAction} />,
    )
    fireEvent.click(actionChip())
    expect(onFollowUpAction).toHaveBeenCalledWith(CLOSE, '7')
  })

  it('blocks the chip while the composer holds text', () => {
    const onFollowUpAction = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} value="half a reply" followUpOptions={[]} onFollowUpSelect={vi.fn()}
        followUpAction={CLOSE} onFollowUpAction={onFollowUpAction} />,
    )
    expect(actionChip()).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(actionChip())
    expect(onFollowUpAction).not.toHaveBeenCalled()
  })

  it('renders no chip when the host gives no way to run it', () => {
    renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['A']} onFollowUpSelect={vi.fn()} followUpAction={CLOSE} />)
    expect(actionChip()).toBeNull()
  })
})

describe('slotUnsentWorkSource', () => {
  it('names a side-pane draft for the slot', () => {
    writeSideDraft('side-1', 'slot-a', 'draft text')
    expect(slotUnsentWorkSource('slot-a')).toBe('elsewhere')
  })

  it('reports nothing for a clean slot', () => {
    expect(slotUnsentWorkSource('slot-a')).toBeNull()
  })
})

describe('closeFromActionChip', () => {
  const store = () => createTestStore()

  it('closes the slot through the store', async () => {
    const del = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    const s = store()
    await act(async () => { await s.dispatch(closeFromActionChip('slot-a', '7', () => '7')) })
    expect(del).toHaveBeenCalledWith('slot-a')
  })

  it('refuses a click made on an older offer and closes nothing', async () => {
    const del = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    await expect(store().dispatch(closeFromActionChip('slot-a', '6', () => '7'))).rejects.toThrow("Couldn't close the session")
    expect(del).not.toHaveBeenCalled()
  })

  it("rejects with the sidebar's own refusal sentence when the server refuses", async () => {
    vi.spyOn(api, 'deleteChatSlot').mockRejectedValue(new Error('boom'))
    vi.spyOn(api, 'chatSlots').mockResolvedValue([{ key: 'slot-a', title: '', messages: 1, running: false }] as never)
    await expect(store().dispatch(closeFromActionChip('slot-a', '7', () => '7'))).rejects.toThrow("“slot-a” is still open: the close didn't go through.")
  })

  const seeded = () => {
    const s = store()
    s.dispatch(addSlotOptimistic({ key: 'slot-a', history_key: 'dashboard:slot-a', title: 'A' } as unknown as ChatSlot))
    return s
  }
  const neverOpen = () => false

  it('records the chip close so the reopen shortcut brings it back', async () => {
    resetRecentlyClosedSessions()
    vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    await act(async () => { await seeded().dispatch(closeFromActionChip('slot-a', '7', () => '7')) })
    expect(takeRecentlyClosedSession(neverOpen)).toEqual({ key: 'slot-a', historyKey: 'dashboard:slot-a', title: 'A' })
  })

  it('drops the record when the server refuses the close', async () => {
    resetRecentlyClosedSessions()
    vi.spyOn(api, 'deleteChatSlot').mockRejectedValue(new Error('boom'))
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    await expect(seeded().dispatch(closeFromActionChip('slot-a', '7', () => '7'))).rejects.toThrow()
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })
})

describe('closeFromActionChip guards', () => {
  it('refuses when the offer moved on during the handshake, and closes nothing', async () => {
    const del = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    const keys = ['7', '8']
    await expect(createTestStore().dispatch(closeFromActionChip('slot-a', '7', () => keys.shift() ?? '8')))
      .rejects.toThrow("Couldn't close the session")
    expect(del).not.toHaveBeenCalled()
  })

  it('asks first on the shared close rule, and a cancel closes nothing without failing', async () => {
    const del = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    vi.mocked(closeConfirmPrompt).mockReturnValue({ title: 't', body: 'b', confirmLabel: 'c' } as never)
    vi.mocked(askAppConfirm).mockResolvedValue(false)
    await createTestStore().dispatch(closeFromActionChip('slot-a', '7', () => '7'))
    expect(closeConfirmPrompt).toHaveBeenCalledWith(expect.anything(), 'slot-a', expect.objectContaining({ settingOn: expect.any(Boolean) }))
    expect(del).not.toHaveBeenCalled()
  })

  it('closes once the prompt is confirmed', async () => {
    const del = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    vi.mocked(closeConfirmPrompt).mockReturnValue({ title: 't', body: 'b', confirmLabel: 'c' } as never)
    await createTestStore().dispatch(closeFromActionChip('slot-a', '7', () => '7'))
    expect(del).toHaveBeenCalledWith('slot-a')
  })

  it('refuses while another pane of this window holds work for the slot', async () => {
    const del = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue(undefined as never)
    const pane = renderHook(() => useSlotWorkBeacon('slot-a', true))
    await expect(createTestStore().dispatch(closeFromActionChip('slot-a', '7', () => '7'))).rejects.toThrow()
    expect(del).not.toHaveBeenCalled()
    pane.unmount()
  })
})

describe('useSlotWorkBeacon', () => {
  it('claims the slot while dirty and retracts when clean', () => {
    const hook = renderHook(({ dirty }) => useSlotWorkBeacon('slot-a', dirty), { initialProps: { dirty: true } })
    expect(localComposerHoldsWork('slot-a')).toBe(true)
    expect(anyWindowSlotDirty('slot-a')).toBe(true)
    expect(slotUnsentWorkSource('slot-a')).toBe('elsewhere')
    hook.rerender({ dirty: false })
    expect(localComposerHoldsWork('slot-a')).toBe(false)
    expect(anyWindowSlotDirty('slot-a')).toBe(false)
    hook.unmount()
  })

  it('vetoes a close another window announces while dirty', () => {
    const hook = renderHook(() => useSlotWorkBeacon('slot-a', true))
    const nonce = publishClosingIntent('slot-a')!
    expect(closingIntentVetoed(nonce)).toBe(false)
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: `${CLOSING_KEY_PREFIX}intent:slot-a:${nonce}` })) })
    expect(closingIntentVetoed(nonce)).toBe(true)
    hook.unmount()
  })

  it('vetoes a close already in flight when it turns dirty', () => {
    const nonce = publishClosingIntent('slot-a')!
    const hook = renderHook(() => useSlotWorkBeacon('slot-a', true))
    expect(closingIntentVetoed(nonce)).toBe(true)
    hook.unmount()
  })

  it('drops its presence stamp when the page unloads, since no cleanup runs then', () => {
    const hook = renderHook(() => useSlotWorkBeacon('slot-a', false))
    const key = `${CLOSING_KEY_PREFIX}present:${hook.result.current}`
    expect(localStorage.getItem(key)).not.toBeNull()
    act(() => { window.dispatchEvent(new Event('beforeunload')) })
    expect(localStorage.getItem(key)).toBeNull()
    hook.unmount()
  })

  it('CONTROL: a clean composer vetoes nothing', () => {
    const hook = renderHook(() => useSlotWorkBeacon('slot-a', false))
    const nonce = publishClosingIntent('slot-a')!
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: `${CLOSING_KEY_PREFIX}intent:slot-a:${nonce}` })) })
    expect(closingIntentVetoed(nonce)).toBe(false)
    hook.unmount()
  })
})

describe('a composer never blocks the chip on its own claim', () => {
  it('slotUnsentWorkSource skips the asking composer, in this window and across windows', () => {
    const hook = renderHook(() => useSlotWorkBeacon('slot-a', true))
    expect(slotUnsentWorkSource('slot-a')).toBe('elsewhere')
    expect(slotUnsentWorkSource('slot-a', hook.result.current)).toBeNull()
    expect(anyWindowSlotDirty('slot-a', hook.result.current)).toBe(false)
    hook.unmount()
  })

  it('re-enables the chip in the render where the user clears their draft', () => {
    const props = { ...defaultProps, followUpOptions: [], onFollowUpSelect: vi.fn(), followUpAction: CLOSE, onFollowUpAction: vi.fn() }
    const ui = (value: string) => <SlotProvider slotId="slot-a"><ChatInput {...props} value={value} /></SlotProvider>
    const view = renderWithProviders(ui('x'))
    expect(actionChip()).toHaveAttribute('aria-disabled', 'true')
    view.rerender(ui(''))
    expect(actionChip()).toHaveAttribute('aria-disabled', 'false')
    expect(document.body.textContent).not.toMatch(/other pane/i)
  })
})

describe('the chip follows another pane of this window without a render of its own', () => {
  const props = { ...defaultProps, value: '', followUpOptions: [], onFollowUpSelect: vi.fn(), followUpAction: CLOSE, onFollowUpAction: vi.fn() }
  const mount = () => renderWithProviders(<SlotProvider slotId="slot-a"><ChatInput {...props} /></SlotProvider>)

  it('re-enables when the other pane clears its draft', () => {
    mount()
    const pane = renderHook(({ dirty }) => useSlotWorkBeacon('slot-a', dirty), { initialProps: { dirty: true } })
    expect(actionChip()).toHaveAttribute('aria-disabled', 'true')
    pane.rerender({ dirty: false })
    expect(actionChip()).toHaveAttribute('aria-disabled', 'false')
    pane.unmount()
  })

  it('blocks and re-enables as a side draft is written and cleared', () => {
    mount()
    expect(actionChip()).toHaveAttribute('aria-disabled', 'false')
    act(() => { writeSideDraft('side-1', 'slot-a', 'draft text') })
    expect(actionChip()).toHaveAttribute('aria-disabled', 'true')
    act(() => { clearSideDraft('side-1') })
    expect(actionChip()).toHaveAttribute('aria-disabled', 'false')
  })
})
