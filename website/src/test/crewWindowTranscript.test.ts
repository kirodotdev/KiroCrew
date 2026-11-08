/**
 * A batch transcript for a crew window's voice key that settles after the
 * window closed (or switched between draft and rewind) lands in that crew
 * session's own draft, the store the window reads on reopen, never in a local
 * draft no screen shows.
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
    expect(deliverCrewWindowTranscript(crewWindowSlot(target.instanceId, target.key), append('then spoken'))).toBe(true)
    expect(readCrewDraft(target)).toBe('typed first then spoken')
  })

  it('keeps a rewind-key transcript in the same session\'s draft', () => {
    expect(deliverCrewWindowTranscript(crewWindowSlot(target.instanceId, target.key) + ':rewind', append('from rewind'))).toBe(true)
    expect(readCrewDraft(target)).toBe('from rewind')
  })

  it('leaves a local slot key to the caller', () => {
    expect(deliverCrewWindowTranscript('chat-3', append('local'))).toBe(false)
    expect(deliverCrewWindowTranscript('crew-window:not-json', append('x'))).toBe(false)
    expect(readCrewDraft(target)).toBe('')
  })
})
