// The shared-log wipe is gated on a FULLY clean clear, so a partial leaves the messages while
// the confirm the user accepted promised deletion, and nothing else corrects that.
import { describe, expect, it } from 'vitest'

import { clearContextBusyMessage } from '../pages/ChannelPage'
import en from '../i18n/locales/en.manual.json'

const KEPT = (en as { pages: { channelPage: Record<string, string> } })
  .pages.channelPage.clear_context_messages_kept

describe('the partial clear-all banner', () => {
  it('states that the channel messages were kept', () => {
    const msg = clearContextBusyMessage({ cleared: ['alpha'], busy: ['beta'] })
    expect(msg).toContain(KEPT)
  })

  it('names the cleared and the refused roles alongside it', () => {
    const msg = clearContextBusyMessage({ cleared: ['alpha'], busy: ['beta'] })
    expect(msg).toContain('alpha')
    expect(msg).toContain('beta')
  })

  it('states it on a total refusal too, which is where the confirm left the question open', () => {
    // The CONFIRM raised it, not the banner: the user accepted copy promising deletion, so
    // omitting the messages' fate leaves the transcript's survival unstated.
    const msg = clearContextBusyMessage({ cleared: [], busy: ['beta'] })
    expect(msg).toContain(KEPT)
    expect(msg).toContain('beta')
  })

  it('states the transcript went with an all-scope clear, and only then', () => {
    // The emptied transcript would otherwise be the only witness that the clear also
    // deleted it; a per-agent clear keeps it, so the same line there would be a lie.
    const all = (en as { pages: { channelPage: Record<string, string> } })
      .pages.channelPage.clear_context_messages_deleted
    expect(all).toBeTruthy()
    expect(all).not.toEqual(KEPT)
  })

  it('omits the messages line for a per-agent refusal', () => {
    // That confirm already promises the shared messages are preserved, so repeating it here
    // implies clearing ONE agent could have deleted the transcript.
    const msg = clearContextBusyMessage({ cleared: [], busy: ['beta'] }, 'agent')
    expect(msg).not.toContain(KEPT)
    expect(msg).toContain('beta')
  })

  it('renders nothing at all when the clear was complete', () => {
    expect(clearContextBusyMessage({ cleared: ['alpha'], busy: [] })).toBe('')
  })

  it('carries the fact in every locale, not only the source catalog', async () => {
    const locales = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
    for (const name of locales) {
      const cat = await import(`../i18n/locales/${name}.json`)
      const value = cat.default?.pages?.channelPage?.clear_context_messages_kept
      expect(value, `${name} is missing the messages-kept fact`).toBeTruthy()
      // Not the English string copied across: an untranslated value would leave the one
      // sentence on this banner in a different language from the two beside it.
      expect(value, `${name} carries the untranslated English value`).not.toBe(KEPT)
    }
  })
})
