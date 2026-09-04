/** Tests for the cron create form's cheaper-mode hint.
 *
 *  The hint nudges a user toward a mode that changes what their job can see at
 *  run time, so the tests pin the direction of every wrong answer:
 *
 *  * A prompt that reasons must never be sent down the script path. That failure
 *    hides -- the job stays green and quietly stops doing its work.
 *  * A prompt that reads its injected context must get no hint at all, because
 *    both cheaper modes would break it.
 *  * A half-typed prompt must stay silent, so the hint does not flicker in and
 *    out while someone is still writing the first few words.
 */

import { describe, expect, it } from 'vitest'

import { adviseCronMode } from '../utils/cronModeAdvice'
import { SCHEDULE_PRESETS } from '../utils/schedulePresets'

describe('adviseCronMode', () => {
  it('stays silent until enough has been typed to judge', () => {
    expect(adviseCronMode('', false)).toBe('none')
    expect(adviseCronMode('check', false)).toBe('none')
    expect(adviseCronMode('   disk   ', false)).toBe('none')
  })

  it('suggests a script for mechanical work', () => {
    for (const prompt of [
      'Compare the channel timestamp with the last run and report a change.',
      'Check whether disk usage is above 80 percent.',
      'Report the http status of the health endpoint.',
      'Tell me when the file size exceeds the quota.',
      'Check the exit code of the nightly job.',
    ]) {
      expect(adviseCronMode(prompt, false), prompt).toBe('script')
    }
  })

  it('still suggests a script when minimal context is already on, because it is cheaper', () => {
    expect(adviseCronMode('Check whether the timestamp changed.', true)).toBe('script')
  })

  it('never suggests a script for work that reasons', () => {
    for (const prompt of [
      'Summarize the new messages and check the timestamp.',
      'Review the disk usage and tell me what to do.',
      'Draft a note about the exit code.',
      'Triage anything above 5 and reply.',
      'Investigate the checksum mismatch.',
      'Recommend a new threshold.',
    ]) {
      expect(adviseCronMode(prompt, false), prompt).toBe('minimal-context')
    }
  })

  it('suggests minimal context for a reasoning job that does not need the full context', () => {
    expect(adviseCronMode('Summarize the week and post it.', false)).toBe('minimal-context')
  })

  it('says nothing once minimal context is on and no script is possible', () => {
    expect(adviseCronMode('Summarize the week and post it.', true)).toBe('none')
  })

  it('says nothing when the job reads context a cheaper mode would drop', () => {
    for (const prompt of [
      'Using my saved preferences, decide what to escalate.',
      'Remember what we agreed and check the timestamp.',
      'Apply the lessons from the previous session.',
      'Follow the project context when reporting disk usage.',
    ]) {
      expect(adviseCronMode(prompt, false), prompt).toBe('none')
    }
  })

  it('says nothing when the prompt drives a skill, by token or by name', () => {
    expect(adviseCronMode('Run $babysit on the open pull request.', false)).toBe('none')
    expect(adviseCronMode('Load the prepare-pr skill and check the exit code.', false)).toBe('none')
  })

  it('says nothing about an ordinary prompt with no signal either way', () => {
    expect(adviseCronMode('Post the standup reminder to the team.', false)).toBe('none')
  })

  it('tolerates a message that is not a string', () => {
    expect(adviseCronMode(undefined as unknown as string, false)).toBe('none')
  })
})

describe('the hint against the shipped schedule templates', () => {
  /** Every template the product ships is a real prompt someone will create a job
   *  from, so the whole preset set is a free calibration corpus. Both halves of
   *  this matter: a template steered toward a script would break a job the
   *  product itself recommended, and a hint that fires on nothing would be dead
   *  code dressed up as a feature. */
  it('never steers a shipped template toward a script, and still helps most of them', () => {
    const verdicts = SCHEDULE_PRESETS.map(p => ({
      id: p.id,
      verdict: adviseCronMode(p.prefill.message, false),
    }))

    const wrong = verdicts.filter(v => v.verdict === 'script').map(v => v.id)
    expect(wrong, 'a shipped template must never be told to become a script').toEqual([])

    const helped = verdicts.filter(v => v.verdict === 'minimal-context')
    expect(helped.length).toBeGreaterThan(verdicts.length / 2)
  })
})
