import { afterEach, describe, expect, it } from 'vitest'
import {
  MAX_HELD, clearSlotUploadFailures, forgetSlotUploadFailure, holdSlotUploadFailure, takeSlotUploadFailure,
} from './slotUploadFailures'

afterEach(() => { clearSlotUploadFailures() })

describe('held upload failures', () => {
  it('drops a chat once its failure is taken', () => {
    holdSlotUploadFailure('a', 'failed')
    expect(takeSlotUploadFailure('a')).toBe('failed')
    expect(takeSlotUploadFailure('a')).toBe('')
  })

  it('drops a closed chat', () => {
    holdSlotUploadFailure('a', 'failed')
    forgetSlotUploadFailure('a')
    expect(takeSlotUploadFailure('a')).toBe('')
  })

  it('keeps only the newest entries, a re-held chat counting as new', () => {
    for (let i = 0; i <= MAX_HELD; i++) holdSlotUploadFailure(`s${i}`, `m${i}`)
    // s0 was the oldest when the table went one past the cap.
    expect(takeSlotUploadFailure('s0')).toBe('')
    holdSlotUploadFailure('s1', 'again')
    holdSlotUploadFailure('extra', 'x')
    // s1 was refreshed, so s2 is now the oldest and goes.
    expect(takeSlotUploadFailure('s2')).toBe('')
    expect(takeSlotUploadFailure('s1')).toBe('again')
    expect(takeSlotUploadFailure(`s${MAX_HELD}`)).toBe(`m${MAX_HELD}`)
  })
})
