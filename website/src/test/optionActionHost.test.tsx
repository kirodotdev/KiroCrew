// The first production host of the close chip: the tag reaches the bar through
// `deriveFollowUpOptions` and `ChatInput`.
import { describe, it, expect, vi } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { defaultProps } from './ChatInputTestUtils'
import { deriveFollowUpOptions } from '../app-sdk/protocol/options'
import { optionActionOf, stripOptionActionTag, type OfferedAction } from '../app-sdk/protocol/optionActionTag'
import { isStringTagLine } from '../app-sdk/protocol/keepVisibleMarker'
import type { ChatMessage } from '../types'

const assistant = (content: string, ts = '1'): ChatMessage =>
  ({ role: 'assistant', content, ts }) as unknown as ChatMessage
const CLOSE: OfferedAction = { action: 'close', label: 'All done' }
const actionChip = () => document.querySelector('[data-option-action]') as HTMLElement

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

  it('reads a tag line exactly as isStringTagLine does, so the chip and the backend strip agree', () => {
    const lines = [
      `\t${tag('close=Done')}`,
      `   ${tag('close=Done')}`,
      `    ${tag('close=Done')}`,
      `<!--${' '.repeat(16)}option-action:close=Done -->`,
      `<!--${' '.repeat(17)}option-action:close=Done -->`,
      `${tag('close=Done')}${' '.repeat(16)}`,
      `${tag('close=Done')}${' '.repeat(17)}`,
    ]
    for (const line of lines) {
      expect(optionActionOf(`Done.\n${line}`) !== null, JSON.stringify(line)).toBe(isStringTagLine(line))
    }
    expect(optionActionOf(`Done.\n\t${tag('close=Done')}`)).toEqual({ action: 'close', label: 'Done' })
    expect(stripOptionActionTag(`Done.\n\t${tag('close=Done')}`)).toBe('Done.')
  })

  it('skips only recognised control-tag lines after the tag', () => {
    expect(optionActionOf(`Done.\n${tag('close=Done')}\n<!-- keep-visible -->`)).toEqual({ action: 'close', label: 'Done' })
    expect(optionActionOf(`Done.\n${tag('close=Done')}\n<!-- plan_task_id:7 -->`)).toEqual({ action: 'close', label: 'Done' })
    expect(optionActionOf(`Done.\n${tag('close=Done')}\n<!-- a note -->`)).toBeNull()
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

  it('offers no close across a failed newer request, which is still owed', () => {
    const user = { role: 'user', content: 'also bump the version', ts: '2' } as unknown as ChatMessage
    const error = { role: 'error', content: 'turn failed', ts: '3' } as unknown as ChatMessage
    const d = deriveFollowUpOptions([assistant(`Done.\n${tag('close=PR merged')}`), user, error], false)
    expect(d.followUpAction).toBeNull()
    expect(d.followUpOptions).toEqual([])
    const withOptions = deriveFollowUpOptions([assistant(`Pick.\n[OPTIONS: A | B]\n${tag('close=PR merged')}`), user, error], false)
    expect(withOptions.followUpOptions).toEqual(['A', 'B'])
    expect(withOptions.followUpAction).toBeNull()
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
    expect(onFollowUpAction).toHaveBeenCalledWith(CLOSE)
  })

  it('keeps the chip live over an unsent draft, which a close keeps', () => {
    const onFollowUpAction = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} value="half a reply" followUpOptions={[]} onFollowUpSelect={vi.fn()}
        followUpAction={CLOSE} onFollowUpAction={onFollowUpAction} />,
    )
    fireEvent.click(actionChip())
    expect(onFollowUpAction).toHaveBeenCalledTimes(1)
  })

  it('passes the host second-click rule through to the chip', () => {
    const onFollowUpAction = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} followUpOptions={[]} onFollowUpSelect={vi.fn()}
        followUpAction={CLOSE} onFollowUpAction={onFollowUpAction} followUpActionNeedsSecondClick={() => true} />,
    )
    fireEvent.click(actionChip())
    expect(onFollowUpAction).not.toHaveBeenCalled()
    fireEvent.click(actionChip())
    expect(onFollowUpAction).toHaveBeenCalledTimes(1)
  })

  it('renders no chip when the host gives no way to run it', () => {
    renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['A']} onFollowUpSelect={vi.fn()} followUpAction={CLOSE} />)
    expect(actionChip()).toBeNull()
  })
})
