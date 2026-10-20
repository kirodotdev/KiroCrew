import { describe, it, expect } from 'vitest'
import { forkFailureMessageForCode } from '../utils/forkFailure'

describe('forkFailureMessageForCode', () => {
  it('names an empty tail with the same words the held menu entry shows', () => {
    expect(forkFailureMessageForCode('no_messages_after_fork_point', 'no messages after the fork point', 'tail'))
      .toBe('Fork failed: No messages after this one yet.')
  })

  it('keeps the raw server text for a code it does not name', () => {
    expect(forkFailureMessageForCode('fork_message_not_found', 'the selected message is no longer present in this session', 'tail'))
      .toBe('Fork failed: the selected message is no longer present in this session')
  })

  it('names the control the caller passes in the too-large advice, and the plain entry otherwise', () => {
    expect(forkFailureMessageForCode('fork_corpus_too_large', '', 'head', 'transcript', undefined, 'Fork with earlier messages'))
      .toContain('choose “Fork with earlier messages”')
    expect(forkFailureMessageForCode('fork_corpus_too_large', '', 'head'))
      .toContain('choose “Fork conversation from here”')
  })
})
