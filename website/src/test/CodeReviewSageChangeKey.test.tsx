import { describe, it, expect } from 'vitest'

import { changeKey, runCoversChange } from '../apps/code-review-sage/lib/format'

/**
 * A PR must be matched to its run by URL, never by `change_id`.
 *
 * `change_id` is produced by the backend's `change_id_for`, which doubles as an
 * on-disk filename: `github_change_id` emits `GH-<owner>-<repo>-<n>` and runs each
 * segment through `_sanitize_seg`, which turns every non-`[A-Za-z0-9.]` character
 * — `-` included — into `_` so that `-` stays unambiguous as the delimiter. Two
 * different repositories therefore share one id: `acme/service-api#5` and
 * `acme/service_api#5` both become `GH-acme-service_api-5`. The backend already
 * hit this — it is why the durable reviewed-index uses `reviewed_key_for`
 * instead — but the UI kept matching on the lossy id, so a collision selected
 * the wrong run, scoped the PR pane to an unrelated review, and posted comments
 * to the other pull request.
 */
describe('matching a PR to its run', () => {
  const hyphen = 'https://github.com/acme/service-api/pull/5'
  const underscore = 'https://github.com/acme/service_api/pull/5'
  // What the backend's `github_change_id` actually emits for BOTH of the URLs
  // above, verified against `sage_lib.adapters`: `_sanitize_seg` turns every
  // non-`[A-Za-z0-9.]` character — `-` included — into `_`, deliberately, so that
  // `-` stays unambiguous as the `GH-<owner>-<repo>-<n>` delimiter. Recorded as a
  // constant rather than recomputed here: re-implementing the sanitizer in the
  // test would only prove the re-implementation.
  const COLLIDING_ID = 'GH-acme-service_api-5'

  it('the two repos really do collide on one change_id', () => {
    // The premise of the bug. Matching on this id cannot tell the repos apart,
    // which is why the match has to be on the URL instead.
    const run = { change_ids: [COLLIDING_ID], changes: [hyphen] }
    expect(run.change_ids.includes(COLLIDING_ID)).toBe(true)
    // ...and the id a client would look up for the OTHER repo is the same string.
    expect(COLLIDING_ID).toBe('GH-acme-service_api-5')
  })

  it('does not confuse two repos that differ only by - vs _', () => {
    const run = { changes: [hyphen] }
    expect(runCoversChange(run, hyphen)).toBe(true)
    // The bug: matching on the shared change_id returned true here.
    expect(runCoversChange(run, underscore)).toBe(false)
  })

  it('matches the same PR across trailing-slash and case differences', () => {
    // The same PR arrives from the picker and from a pasted link.
    const run = { changes: ['https://github.com/acme/widgets/pull/7'] }
    expect(runCoversChange(run, 'https://github.com/acme/widgets/pull/7/')).toBe(true)
    expect(runCoversChange(run, 'https://GitHub.com/acme/widgets/pull/7')).toBe(true)
    expect(runCoversChange(run, '  https://github.com/acme/widgets/pull/7  ')).toBe(true)
  })

  it('does not match a different PR number in the same repo', () => {
    const run = { changes: ['https://github.com/acme/widgets/pull/7'] }
    expect(runCoversChange(run, 'https://github.com/acme/widgets/pull/70')).toBe(false)
    expect(runCoversChange(run, 'https://github.com/acme/widgets/pull/8')).toBe(false)
  })

  it('finds the right change in a multi-PR run', () => {
    const run = { changes: [hyphen, 'https://github.com/acme/widgets/pull/7'] }
    expect(runCoversChange(run, 'https://github.com/acme/widgets/pull/7')).toBe(true)
    expect(runCoversChange(run, underscore)).toBe(false)
  })

  it('is false for a run with no changes and for an empty url', () => {
    expect(runCoversChange({}, hyphen)).toBe(false)
    expect(runCoversChange({ changes: [] }, hyphen)).toBe(false)
    expect(runCoversChange({ changes: [hyphen] }, '')).toBe(false)
    // An empty key must never match an empty entry either.
    expect(runCoversChange({ changes: [''] }, '')).toBe(false)
  })

  it('changeKey normalizes only case and trailing slashes', () => {
    expect(changeKey('https://github.com/o/r/pull/1/')).toBe('https://github.com/o/r/pull/1')
    expect(changeKey('HTTPS://GITHUB.COM/O/R/PULL/1')).toBe('https://github.com/o/r/pull/1')
    // It must NOT collapse punctuation — that is the lossiness being avoided.
    expect(changeKey('https://github.com/acme/service-api/pull/5'))
      .not.toBe(changeKey('https://github.com/acme/service_api/pull/5'))
  })
})
