/**
 * What the transcript's live region says for a reply.
 *
 * Every case here is a construct whose SOURCE contains punctuation the rendered
 * bubble does not show — the thing that made announcing `content` directly
 * unusable. The assertions are on the spoken string, so they fail if any of that
 * punctuation ever reaches a reader.
 */
import { describe, it, expect, vi } from 'vitest'

import { messageAnnouncementText } from './messageAnnouncement'
import { capWhitespaceRuns } from './markdownDepthBound'

// Spied, not replaced: the real cap still runs, so every case below reads the
// text the renderer would, and the cap test can check that it ran first.
vi.mock('./markdownDepthBound', async (importOriginal) => {
  const real = await importOriginal<typeof import('./markdownDepthBound')>()
  return { ...real, capWhitespaceRuns: vi.fn(real.capWhitespaceRuns) }
})

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

  it('reads a reference-style image as its alt text', () => {
    const md = '![Deployment diagram][diagram]\n\n[diagram]: /diagram.png'
    expect(messageAnnouncementText(md)).toBe('Deployment diagram')
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

  it('reads an unknown tag as the literal text the bubble shows', () => {
    // remarkVerbatimUnknownTags turns it into text in the renderer too.
    expect(messageAnnouncementText('Use <dynamoDBClient> here')).toBe('Use <dynamoDBClient> here')
  })

  it("reads a raw image as its alt text, as the bubble's image announces", () => {
    expect(messageAnnouncementText('See <img src="x.png" alt="the chart"> above')).toBe('See the chart above')
  })

  it('reads a block of unclosed image tags as the bubble draws it: nothing', () => {
    // A backtracking tag regex took about 22 s on this 24 KB block. A teammate
    // or channel can send it, so it must stay cheap; the HTML parser drops an
    // unclosed tag, and the bubble draws no text for it either.
    const md = '<div>' + '<img alt="" '.repeat(2000)
    expect(messageAnnouncementText(md)).toBe('')
  })

  it('decodes character references in raw HTML and its alt text, as the bubble shows them', () => {
    expect(messageAnnouncementText('<div><img alt="A &amp; B"> Tom &amp; Jerry</div>')).toBe('A & B Tom & Jerry')
  })

  it('reads a closed raw details block as its summary only, and an open one in full', () => {
    expect(messageAnnouncementText('<details><summary>Logs</summary>secret body</details>')).toBe('Logs')
    expect(messageAnnouncementText('<details open><summary>Logs</summary>shown body</details>')).toBe('Logs shown body')
  })

  it('hides a closed details body written with blank lines, the usual markdown form', () => {
    // A blank line splits the block into sibling nodes; the body is still inside.
    expect(messageAnnouncementText('Before\n\n<details>\n<summary>Logs</summary>\n\nsecret **body**\n\n</details>\n\nAfter')).toBe('Before\nLogs\nAfter')
    expect(messageAnnouncementText('<details open>\n<summary>Logs</summary>\n\nshown body\n\n</details>')).toContain('shown body')
  })

  it('leaves out raw HTML hidden from assistive technology', () => {
    expect(messageAnnouncementText('<div>shown <span aria-hidden="true">decor</span><span hidden>gone</span>text</div>')).toBe('shown text')
  })

  it('leaves out raw HTML a class hides at every width, and keeps sr-only and prefixed classes', () => {
    const html = '<div>shown <span class="x hidden">approve this</span><span class="invisible">a</span>'
      + '<span class="collapse">b</span><span class="sr-only">label</span> <span class="md:hidden">wide</span> text</div>'
    expect(messageAnnouncementText(html)).toBe('shown label wide text')
  })

  it('reads an image inside a raw block as its alt text, and drops the other tags', () => {
    expect(messageAnnouncementText('<div><img alt=\'a cat\' src=x><b>hi</b></div>')).toBe('a cat hi')
  })

  it('reads a widget as its title, not its markup', () => {
    const md = 'Here it is:\n\n<mcwidget title="Sales chart">\n<div><canvas></canvas></div>\n</mcwidget>\n\nDone.'
    const spoken = messageAnnouncementText(md)
    expect(spoken).toBe('Here it is:\nSales chart\nDone.')
  })

  it('reads CJK emphasis without its asterisks, as the renderer parses it', () => {
    // CommonMark alone leaves these `**` literal; remark-cjk-friendly makes them emphasis.
    expect(messageAnnouncementText('**中文（带括号）。**这句')).toBe('中文（带括号）。这句')
  })

  it('reads display math as its source, without the dollar fences', () => {
    expect(messageAnnouncementText('Area:\n\n$$x^2$$')).toBe('Area:\nx^2')
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
    // RangeError that takes out the whole transcript render is not. The depth
    // has to be large enough to overflow the stack without the bounds, so the
    // parse takes about a second here; the limit is set well above the default
    // so a slower CI runner cannot turn it into a timeout.
    const deep = `${'> '.repeat(5000)}bottom`
    expect(() => messageAnnouncementText(deep)).not.toThrow()
    const nested = `${'- '.repeat(2000)}leaf`
    expect(() => messageAnnouncementText(nested)).not.toThrow()
  }, 60_000)

  it('caps indentation before parsing, as the bubble renderer does', () => {
    // Deep indentation makes the parser's cost grow with depth times lines, so
    // the text must pass through the cap first. Checked by the call, not by
    // timing a deep input: a timing check passed here in 2.5 s and timed out
    // on a slower CI runner.
    const md = Array.from({ length: 40 }, (_, i) => `${'  '.repeat(i * 8)}- item`).join('\n')
    vi.mocked(capWhitespaceRuns).mockClear()
    expect(messageAnnouncementText(md)).toContain('item')
    expect(capWhitespaceRuns).toHaveBeenCalledWith(md)
  })
})
