/** Two correlations that redacted display text cannot carry.
 *
 *  The server rewrites an edit's content for display, erasing image markers, so two different edits
 *  can render the SAME string. Anything treating that string as identity accepts the wrong edit.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { queuedSendStash, stashQueuedSend, noteLocalQueueEdit, noteAppliedEditRev, editSupersededByNewerRev, forgetAppliedEditRev, settleQueueEditEcho, retireEditedQueueRecord } from './queuedSendStash'

describe('a queue record is correlated by the edit that settled it, not by its display text', () => {
  beforeEach(() => { queuedSendStash.clear() })

  it('retires a record when the entry names a DIFFERENT edit that renders identically', () => {
    stashQueuedSend('q-1', { raw: 'look at @image.png', files: ['/tmp/image.png'], sent: 'look at @image.png' })
    noteLocalQueueEdit('q-1', 'look at @image.png', 'edit-ours')
    expect(settleQueueEditEcho('q-1', 'look at an image', 'edit-ours'),
      'premise: our own edit settles from its id').toBe(true)

    // Another client edits the same entry. Redaction erases the marker, so the display it produces
    // is byte-identical to ours while its raw text and attachments are not.
    retireEditedQueueRecord('q-1', 'look at an image', 'edit-theirs')

    expect(queuedSendStash.get('q-1'),
      'a record kept on display equality answers a later cancel with obsolete text and files')
      .toBeUndefined()
  })

  it('settles a success whose OWN echo landed first, and refuses a superseded one', () => {
    stashQueuedSend('q-rev', { raw: 'look at @image.png', files: ['/tmp/image.png'], sent: 'look at @image.png' })
    noteLocalQueueEdit('q-rev', 'after MY edit', 'edit-mine')

    // This tab's own echo arrives BEFORE its HTTP response and applies revision 5, spending the
    // pending marker -- which is why the response then looked superseded when it was not.
    noteAppliedEditRev('q-rev', 5)
    expect(editSupersededByNewerRev('q-rev', 5),
      'an equal revision is this tab own edit, so its success must still settle').toBe(false)

    // A DIFFERENT client's edit lands after mine, so mine no longer describes the card.
    noteAppliedEditRev('q-rev', 6)
    expect(editSupersededByNewerRev('q-rev', 5),
      'a strictly newer revision means this response names text nothing on screen shows').toBe(true)

    // Monotonic, or a frame arriving out of order would lower the applied revision and a later
    // success would be judged against one already replaced.
    noteAppliedEditRev('q-rev', 2)
    expect(editSupersededByNewerRev('q-rev', 5),
      'an out-of-order frame must not lower what has been applied').toBe(true)
  })

  it('drops the recorded revision when the entry leaves the queue', () => {
    stashQueuedSend('q-gone', { raw: 'text', files: [], sent: 'text' })
    noteAppliedEditRev('q-gone', 4)
    expect(editSupersededByNewerRev('q-gone', 3),
      'premise: revision 4 is recorded, so an older frame is refused').toBe(true)

    forgetAppliedEditRev('q-gone')

    // The map is keyed by queue id for the page's lifetime, so a cancelled entry's revision would
    // otherwise outlive every entry it could ever apply to.
    expect(editSupersededByNewerRev('q-gone', 3),
      'a retired entry keeps no revision to judge a later frame against').toBe(false)
  })

  it('keeps the record when the entry names OUR edit, so the guard is not refusing everything', () => {
    stashQueuedSend('q-2', { raw: 'look at @image.png', files: ['/tmp/image.png'], sent: 'look at @image.png' })
    noteLocalQueueEdit('q-2', 'look at @image.png', 'edit-ours')
    settleQueueEditEcho('q-2', 'look at an image', 'edit-ours')

    retireEditedQueueRecord('q-2', 'look at an image', 'edit-ours')

    expect(queuedSendStash.get('q-2'),
      'the current edit own record is live recovery data, not stale')
      .toBeDefined()
  })

  it('falls back to the display comparison when the entry carries no edit id', () => {
    stashQueuedSend('q-3', { raw: 'look at @image.png', files: ['/tmp/image.png'], sent: 'look at @image.png' })
    noteLocalQueueEdit('q-3', 'look at @image.png', 'edit-ours')
    settleQueueEditEcho('q-3', 'look at an image', 'edit-ours')

    retireEditedQueueRecord('q-3', 'look at an image')

    expect(queuedSendStash.get('q-3'),
      'a server that predates the id must keep behaving as it did').toBeDefined()
  })
})
