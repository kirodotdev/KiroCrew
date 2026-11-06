/**
 * What the transcript's live region says for a reply.
 *
 * Every case here is a construct whose SOURCE contains punctuation the rendered
 * bubble does not show — the thing that made announcing `content` directly
 * unusable. The assertions are on the spoken string, so they fail if any of that
 * punctuation ever reaches a reader.
 */
import { describe, it, expect } from 'vitest'

import { messageAnnouncementText } from './messageAnnouncement'

describe('messageAnnouncementText', () => {
  it('says nothing for an empty or blank message', () => {
    expect(messageAnnouncementText('')).toBe('')
    expect(messageAnnouncementText('   \n\n  ')).toBe('')
  })

  it('drops emphasis, strikethrough and heading markers', () => {
    expect(messageAnnouncementText('## Plan')).toBe('Plan')
    expect(messageAnnouncementText('**done** and *gone* and ~~cut~~'))
      .toBe('done and gone and cut')
  })

  it('keeps a word that emphasis merely interrupted whole', () => {
    // The inline join is deliberately empty rather than a space.
    expect(messageAnnouncementText('re**start**ing')).toBe('restarting')
  })

  it('speaks code contents without the backticks', () => {
    expect(messageAnnouncementText('run `npm ci` first')).toBe('run npm ci first')
    expect(messageAnnouncementText('```ts\nconst a = 1\n```')).toBe('const a = 1')
  })

  it('reads a link and an image as what the rendered element announces', () => {
    // The destination is never spoken; the alt text is.
    expect(messageAnnouncementText('see [the docs](https://example.com/x?a=1)'))
      .toBe('see the docs')
    expect(messageAnnouncementText('![a red square](/img/red.png)')).toBe('a red square')
  })

  it('reads a gfm table as rows of cells, not pipes', () => {
    const md = ['| Name | Count |', '| --- | --- |', '| apples | 2 |'].join('\n')
    expect(messageAnnouncementText(md)).toBe('Name, Count\napples, 2')
  })

  it('reads lists and block quotes as their text', () => {
    expect(messageAnnouncementText('- one\n- two')).toBe('one\ntwo')
    expect(messageAnnouncementText('1. first\n2. second')).toBe('first\nsecond')
    expect(messageAnnouncementText('> quoted')).toBe('quoted')
  })

  it('reads raw html as its text, since rehype-raw renders it', () => {
    expect(messageAnnouncementText('<b>bold</b> thing')).toBe('bold thing')
  })

  it('drops a horizontal rule rather than announcing a separator', () => {
    expect(messageAnnouncementText('before\n\n---\n\nafter')).toBe('before\nafter')
  })

  it('collapses the blank lines between blocks to one separator', () => {
    expect(messageAnnouncementText('one\n\n\n\ntwo')).toBe('one\ntwo')
  })

  it('survives input-controlled nesting instead of overflowing the stack', () => {
    // The walk is recursive, so this is the case remarkBoundDepth plus the
    // walk's own ceiling exist for. A wrong answer is acceptable here; a
    // RangeError that takes out the whole transcript render is not.
    const deep = `${'> '.repeat(5000)}bottom`
    expect(() => messageAnnouncementText(deep)).not.toThrow()
    const nested = `${'- '.repeat(2000)}leaf`
    expect(() => messageAnnouncementText(nested)).not.toThrow()
  })
})
