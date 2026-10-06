/**
 * Mochi's streaming fence repair moves a fence glued to text onto its own line
 * and closes a fence the stream has not closed yet. A ``` run in the middle of
 * prose is not a fence, so neither step may turn it into one.
 */
import { describe, expect, it } from 'vitest'

import { fixStreamingFences } from '../src/renderer/ChatPanel'

describe('fixStreamingFences', () => {
  it('leaves a mid-sentence ``` run and the prose after it unchanged', () => {
    for (const input of [
      'Use a ```diff block for file changes.\nThis line stays prose.',
      '- Type ``` then Enter to start a code block\n- second item',
    ]) expect(fixStreamingFences(input)).toBe(input)
  })

  it('separates and closes a glued fence that is still streaming', () => {
    expect(fixStreamingFences('Here is code:```python\nprint(1)')).toBe('Here is code:\n\n```python\nprint(1)\n```')
  })
})
