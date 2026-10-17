/**
 * `focusComposerAfterPick` reports where focus LANDED, not that it was asked
 * for (#18313, and the Opus finding on PR #18321): a `true` suppresses the
 * picker's own return-to-trigger, so a `true` with focus somewhere else would
 * strand focus on <body>.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ComposerControl } from '../components/composerControl'

const flags = { touch: false }
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => flags.touch }))

import { focusComposerAfterPick } from '../components/chat-input/pickerFocus'

function controlFor(el: HTMLElement | null): ComposerControl {
  return {
    focus: () => el?.focus(),
    getRootElement: () => el,
    getSelection: () => null,
    setSelection: () => {},
  }
}

let textarea: HTMLTextAreaElement
let trigger: HTMLButtonElement

beforeEach(() => {
  flags.touch = false
  textarea = document.createElement('textarea')
  trigger = document.createElement('button')
  document.body.append(textarea, trigger)
  trigger.focus()
})

afterEach(() => {
  textarea.remove()
  trigger.remove()
})

describe('focusComposerAfterPick', () => {
  it('focuses the live composer and reports true', () => {
    expect(focusComposerAfterPick(controlFor(textarea))).toBe(true)
    expect(textarea).toHaveFocus()
  })

  it('reports false when the composer is disabled, so the picker keeps its trigger return', () => {
    textarea.disabled = true
    expect(focusComposerAfterPick(controlFor(textarea))).toBe(false)
    expect(textarea).not.toHaveFocus()
  })

  it('reports false without a control', () => {
    expect(focusComposerAfterPick(null)).toBe(false)
    expect(trigger).toHaveFocus()
  })

  it('declines on touch without touching focus', () => {
    flags.touch = true
    expect(focusComposerAfterPick(controlFor(textarea))).toBe(false)
    expect(trigger).toHaveFocus()
  })
})
