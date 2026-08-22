import { describe, it, expect } from 'vitest'
import { extractPathHintFromText } from '../components/MarkdownRenderer'

/** The hint labels a diff block and drives its Open button, so a false positive
 *  is not cosmetic: it titles the block with a file that does not exist and
 *  sends a probe after it. The text it reads is ordinary prose, where a
 *  slash-joined pair is far more often a grouping than a path. */
describe('extractPathHintFromText', () => {
  it('ignores a slash-joined grouping in the middle of a sentence', () => {
    // Observed live: this produced "/Beta/Prod", which titled a snippet block.
    expect(extractPathHintFromText('during DARU migration EU-ZAZ shares EU/Beta/Prod accounts')).toBeUndefined()
  })

  it('ignores other mid-prose slashes', () => {
    expect(extractPathHintFromText('it splits 1/2 of the load')).toBeUndefined()
    expect(extractPathHintFromText('choose split/unified from the menu')).toBeUndefined()
  })

  it('ignores an introducer buried inside a longer word', () => {
    // The introducer list is word-bounded: unbounded, `File` fires inside
    // `Profile` / `Dockerfile` and `Created` inside `Recreated`, each handing
    // back a path the sentence never claimed.
    expect(extractPathHintFromText('Profile /tmp/x.ts')).toBeUndefined()
    expect(extractPathHintFromText('Dockerfile /etc/nginx.conf')).toBeUndefined()
    expect(extractPathHintFromText('Recreated /etc/passwd during setup')).toBeUndefined()
  })

  it('still reads a verb-introduced path', () => {
    expect(extractPathHintFromText('Created /home/me/Thing.java:')).toBe('/home/me/Thing.java')
    expect(extractPathHintFromText('Modified `/tmp/x.ts`')).toBe('/tmp/x.ts')
  })

  it('still reads a line that is nothing but a path', () => {
    expect(extractPathHintFromText('/abs/path/file.ts')).toBe('/abs/path/file.ts')
    expect(extractPathHintFromText('~/notes/todo.md')).toBe('~/notes/todo.md')
  })

  it('gives up a mid-sentence path that no introducer announces', () => {
    // The deliberate cost of anchoring: a path mentioned in passing after a verb
    // the list does not carry no longer hints. Recall is traded for precision
    // because a false hint is not inert — it titles the block with a file that
    // does not exist and fires a probe at it, while a missed hint only leaves
    // the block untitled. Pinned so the trade-off cannot be lost silently.
    expect(extractPathHintFromText('see /etc/hosts for details')).toBeUndefined()
    expect(extractPathHintFromText('check ~/notes/todo.md later')).toBeUndefined()
    // An introducer anywhere in the line still carries it.
    expect(extractPathHintFromText('the file /etc/hosts is shared')).toBe('/etc/hosts')
  })

  it('scans back past the blank line directly above a fence', () => {
    expect(extractPathHintFromText('Wrote /srv/app/main.py\n\n')).toBe('/srv/app/main.py')
  })
})
