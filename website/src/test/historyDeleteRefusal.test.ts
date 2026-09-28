import { describe, it, expect } from 'vitest'

import {
  CRON_OWNERSHIP_UNKNOWN_CODE,
  CRON_STORE_BUSY_CODE,
  CRON_STORE_UNREADABLE_CODE,
  historyDeleteRefusalMessage,
} from '../utils/historyDeleteRefusal'
import type { HistoryDeleteRefusal } from '../utils/historyDeleteRefusal'

/**
 * A refused history delete is rendered from its machine-readable CODE alone --
 * never from the gateway's English error prose -- so each recognised code must
 * reach its OWN catalog sentence (the remedies differ), an unrecognised code
 * must fall to the generic sentence, and a row with no title must still be
 * nameable. The shared test setup pins i18next to English.
 */
function refusal(code: string, title = 'Planning notes'): HistoryDeleteRefusal {
  return { key: 'dashboard:slot-7', title, code }
}

describe('historyDeleteRefusalMessage', () => {
  it('gives each recognised code its own sentence naming the row title', () => {
    const ownership = historyDeleteRefusalMessage(refusal(CRON_OWNERSHIP_UNKNOWN_CODE))
    const busy = historyDeleteRefusalMessage(refusal(CRON_STORE_BUSY_CODE))
    const unreadable = historyDeleteRefusalMessage(refusal(CRON_STORE_UNREADABLE_CODE))
    const sentences = [ownership, busy, unreadable]

    sentences.forEach((sentence) => {
      expect(sentence).toContain('"Planning notes"')
      // A missing catalog entry makes i18nT hand the dotted KEY back.
      expect(sentence).not.toMatch(/^pages\.chatPage\./)
    })
    // Three causes, three remedies, three distinct sentences.
    expect(new Set(sentences).size).toBe(3)
    expect(ownership).toContain('cron adopt')
    expect(ownership).toContain('repair the transcript metadata')
    expect(busy).toContain('Try again')
    expect(unreadable).toContain('Repair the cron store')
  })

  it('falls to the generic sentence when the code is not recognised', () => {
    const generic = historyDeleteRefusalMessage(refusal('not_a_known_code'))
    expect(generic).toBe('Could not delete "Planning notes".')
    // A rejection carrying no code at all (dropped connection, 5xx) is the same case.
    expect(historyDeleteRefusalMessage(refusal(''))).toBe(generic)
  })

  it('names the row by its key when the title is empty', () => {
    const sentence = historyDeleteRefusalMessage(refusal(CRON_STORE_BUSY_CODE, ''))
    expect(sentence).toContain('"dashboard:slot-7"')
    expect(sentence).not.toContain('""')
  })
})
