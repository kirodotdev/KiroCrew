/** An edit that DROPS an attachment marker prunes that file server-side, so the stash must drop it
 *  too or a cancel re-stages it and a resend carries an attachment the user removed.
 *
 *  The retained reference is decided by a TEXT search, and a plain substring test cannot tell a real
 *  mention from a longer path that merely starts the same way.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { queuedSendStash, stashQueuedSend, restashEditedSend } from './queuedSendStash'

describe('a removed attachment is not revived by a longer path that shares its prefix', () => {
  beforeEach(() => { queuedSendStash.clear() })

  it('drops /tmp/report.pdf when only /tmp/report.pdf.bak survives the edit', () => {
    stashQueuedSend('q-1', {
      raw: 'see /tmp/report.pdf and /tmp/report.pdf.bak',
      files: ['/tmp/report.pdf', '/tmp/report.pdf.bak'],
      sent: 'see /tmp/report.pdf and /tmp/report.pdf.bak',
    })

    // The user removed the shorter attachment; only the .bak reference remains in the edited text.
    restashEditedSend('q-1', 'see /tmp/report.pdf.bak', 'see /tmp/report.pdf.bak')

    const kept = queuedSendStash.get('q-1')?.files ?? []
    expect(kept, 'the surviving attachment must still be carried').toContain('/tmp/report.pdf.bak')
    expect(kept,
      'a removed attachment revived by a prefix match rides along on the next resend')
      .not.toContain('/tmp/report.pdf')
  })

  it('drops /tmp/report.pdf when only /home/user/tmp/report.pdf survives the edit', () => {
    stashQueuedSend('q-3', {
      raw: 'see /tmp/report.pdf and /home/user/tmp/report.pdf',
      files: ['/tmp/report.pdf', '/home/user/tmp/report.pdf'],
      sent: 'see /tmp/report.pdf and /home/user/tmp/report.pdf',
    })

    // The removed path is a contiguous SUFFIX of the surviving one, so the character AFTER the
    // match is the end of the string -- the one boundary the trailing check accepts.
    restashEditedSend('q-3', 'see /home/user/tmp/report.pdf', 'see /home/user/tmp/report.pdf')

    const kept = queuedSendStash.get('q-3')?.files ?? []
    expect(kept, 'the surviving attachment must still be carried').toContain('/home/user/tmp/report.pdf')
    expect(kept,
      'a removed attachment revived by a SUFFIX match rides along on the next resend')
      .not.toContain('/tmp/report.pdf')
  })

  it('keeps a path that IS mentioned, so the boundary test is not merely refusing everything', () => {
    stashQueuedSend('q-2', {
      raw: 'see /tmp/report.pdf now',
      files: ['/tmp/report.pdf'],
      sent: 'see /tmp/report.pdf now',
    })

    restashEditedSend('q-2', 'see /tmp/report.pdf now', 'see /tmp/report.pdf now')

    expect(queuedSendStash.get('q-2')?.files,
      'a genuinely referenced attachment must survive the edit')
      .toContain('/tmp/report.pdf')
  })
})
