import { describe, it, expect } from 'vitest'

import {
  BULK_NAMED_ROWS,
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

/** The Older Sessions "Delete all": a refused request, or a 200 that
 *  left sessions on disk, renders through the same function. */
function bulk(left: number | null, rows: HistoryDeleteRefusal[] = [], cleared = 0): HistoryDeleteRefusal {
  return { key: '', title: '', code: '', bulk: { left, cleared, rows } }
}

const RETRY = 'To retry, press Delete all again.'

function namedRows(...titles: string[]): HistoryDeleteRefusal[] {
  return titles.map(t => ({ key: `dashboard:${t}`, title: `Row ${t}`, code: '' }))
}

describe('historyDeleteRefusalMessage for Delete all', () => {
  it('says the older sessions were not deleted and to try again when the request was refused', () => {
    expect(historyDeleteRefusalMessage(bulk(null)))
      .toBe('Could not delete the older sessions. Press Delete all to try again.')
    // A code with no bulk remedy is the same generic sentence.
    expect(historyDeleteRefusalMessage({ ...bulk(null), code: 'not_a_known_code' }))
      .toBe(historyDeleteRefusalMessage(bulk(null)))
  })

  it('gives a refusal for a busy cron store the generic sentence', () => {
    expect(historyDeleteRefusalMessage({ ...bulk(null), code: CRON_STORE_BUSY_CODE }))
      .toBe(historyDeleteRefusalMessage(bulk(null)))
  })

  it('keeps the unreadable cron store remedy when the whole request was refused for it', () => {
    const unreadable = historyDeleteRefusalMessage({ ...bulk(null), code: CRON_STORE_UNREADABLE_CODE })
    expect(unreadable).toBe(
      'Could not delete the older sessions: the cron store could not be read, so their cron jobs '
      + 'could not be checked. Repair the cron store, then delete again.',
    )
  })

  it('opens with the count alone when the clear deleted nothing', () => {
    expect(historyDeleteRefusalMessage(bulk(3))).toBe(`3 older sessions were not deleted.\n${RETRY}`)
    expect(historyDeleteRefusalMessage(bulk(2, namedRows('a', 'b'))).split('\n')[0])
      .toBe('2 older sessions were not deleted.')
  })

  it('says how many were deleted when the clear deleted some', () => {
    expect(historyDeleteRefusalMessage(bulk(2, [], 1)))
      .toBe(`2 older sessions were not deleted. 1 was deleted.\n${RETRY}`)
    expect(historyDeleteRefusalMessage(bulk(3, [], 2)))
      .toBe(`3 older sessions were not deleted. 2 were deleted.\n${RETRY}`)
    expect(historyDeleteRefusalMessage(bulk(1, [], 4)))
      .toBe(`1 older session was not deleted. 4 were deleted.\n${RETRY}`)
  })

  it('adds the retry line when only unnamed rows were left', () => {
    expect(historyDeleteRefusalMessage(bulk(1))).toBe(`1 older session was not deleted.\n${RETRY}`)
  })

  it('names at most BULK_NAMED_ROWS rows and adds the retry line for rows past the cap', () => {
    const rows = namedRows('a', 'b', 'c', 'd', 'e')
    const sentence = historyDeleteRefusalMessage(bulk(5, rows))
    expect(sentence).toBe([
      '5 older sessions were not deleted.',
      ...rows.slice(0, BULK_NAMED_ROWS).map(r => historyDeleteRefusalMessage(r)),
      RETRY,
    ].join('\n'))
    rows.slice(BULK_NAMED_ROWS).forEach(r => expect(sentence).not.toContain(`"${r.title}"`))
  })

  it('adds no retry line when every left row is named', () => {
    const rows = namedRows('a', 'b')
    expect(historyDeleteRefusalMessage(bulk(2, rows, 3))).toBe([
      '2 older sessions were not deleted. 3 were deleted.',
      ...rows.map(r => historyDeleteRefusalMessage(r)),
    ].join('\n'))
  })

  it('reads exactly like the single delete when one named row was left and nothing was deleted', () => {
    const row = refusal(CRON_OWNERSHIP_UNKNOWN_CODE)
    expect(historyDeleteRefusalMessage(bulk(1, [row]))).toBe(historyDeleteRefusalMessage(row))
  })

  it('keeps the deleted count when one named row was left and others were deleted', () => {
    const row = refusal(CRON_OWNERSHIP_UNKNOWN_CODE)
    expect(historyDeleteRefusalMessage(bulk(1, [row], 2))).toBe([
      '1 older session was not deleted. 2 were deleted.',
      historyDeleteRefusalMessage(row),
    ].join('\n'))
  })

  it('puts the count, each named row and the retry on their own lines', () => {
    const row = refusal(CRON_OWNERSHIP_UNKNOWN_CODE)
    const sentence = historyDeleteRefusalMessage(bulk(3, [row]))
    expect(sentence.split('\n')).toEqual([
      '3 older sessions were not deleted.',
      historyDeleteRefusalMessage(row),
      RETRY,
    ])
    expect(sentence).toContain('cron adopt')
  })
})
