/**
 * #20398: the Stop button's escalation has ONE name. The shared stop copy used
 * "force stop", "Force kill" and "force reset" for the same press, so a reader
 * could not tell whether they were three actions. Every escalation string now
 * says "force stop", and none says "kill" or "reset".
 */
import { describe, it, expect } from 'vitest'
import { i18nT } from '../i18n/t'

const ESCALATION_KEYS = [
  'components.chatInput.click_again_to_force_stop',
  'components.chatInput.click_again_to_force_stop_resets_session',
  'components.chatInput.force_kill_discards_in_progress_work_and_queued',
  'components.chatInput.force_kill_session_discards_in_progress_work_and',
  'components.chatInput.force_reset_taking_longer_than_expected',
  'components.chatInput.force_reset_session_taking_longer_than_expected',
  'components.chatInput.killing',
  'components.chatInput.killing_session',
  'pages.chat.chatFooter.killing',
]

describe('stop escalation copy (#20398)', () => {
  it.each(ESCALATION_KEYS)('%s names the escalation "force stop" and nothing else', (key) => {
    const text = i18nT(key)
    expect(text).not.toBe(key)
    expect(text).toMatch(/force stop/i)
    expect(text).not.toMatch(/\bkill|\breset/i)
  })

  it('says what stays and what goes in one sentence shape', () => {
    for (const key of [
      'components.chatInput.force_kill_discards_in_progress_work_and_queued',
      'components.chatInput.force_kill_session_discards_in_progress_work_and',
    ]) {
      expect(i18nT(key)).toMatch(/your messages stay; in-progress work and queued messages are discarded/)
    }
  })
})
