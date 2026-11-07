/**
 * A batch transcript for a crew window's voice key that settles after the
 * window closed (or switched between draft and rewind) lands in that crew
 * session's own draft, the store the window reads on reopen, never in a local
 * draft no screen shows. The helper answers with the voice key of the box that
 * received the words, so the dictation record marks that box.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { deliverCrewWindowTranscript, readCrewDraft, writeCrewDraft } from '../pages/chat/crew-window/crewWindowStore'
import { crewWindowSlot } from '../pages/chat/crew-window/crewWindowRenderers'

const target = { instanceId: 'devbox', key: 'chat-7' }
const append = (text: string) => (base: string) => (base ? base + ' ' + text : text)

describe('deliverCrewWindowTranscript', () => {
  beforeEach(() => { writeCrewDraft(target, '') })

  it('appends a closed window\'s transcript to that session\'s crew draft', () => {
    writeCrewDraft(target, 'typed first')
    const key = crewWindowSlot(target.instanceId, target.key)
    expect(deliverCrewWindowTranscript(key, append('then spoken'))).toBe(key)
    expect(readCrewDraft(target)).toBe('typed first then spoken')
  })

  it('keeps a rewind-key transcript in the same session\'s draft, and names the draft box', () => {
    const key = crewWindowSlot(target.instanceId, target.key)
    expect(deliverCrewWindowTranscript(key + ':rewind', append('from rewind'))).toBe(key)
    expect(readCrewDraft(target)).toBe('from rewind')
  })

  it('leaves a local slot key to the caller', () => {
    expect(deliverCrewWindowTranscript('chat-3', append('local'))).toBeNull()
    expect(deliverCrewWindowTranscript('crew-window:not-json', append('x'))).toBeNull()
    expect(readCrewDraft(target)).toBe('')
  })
})
