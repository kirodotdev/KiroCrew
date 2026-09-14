/**
 * `isSafePath` — the shared refusal used by the three side-panel open
 * affordances (DiffBlock's "Open", ToolCallLine's file pill, and the app-sdk
 * ChatMessageList's file chip).
 *
 * The cases that matter are the WINDOWS ones. The helper splits on `/`, but two
 * of its three consumers feed it a path taken straight out of a tool call's JSON
 * input — the path the agent actually operated on — so on Windows it arrives
 * with backslashes and the whole string is one segment. Every comparison is
 * against a segment, so nothing can ever match and the refusal is inert.
 *
 * Each Windows case below is paired with its POSIX twin, because the pairing IS
 * the assertion: the same file must not be refused on one platform and offered
 * on the other.
 */
import { describe, it, expect } from 'vitest'

import { isSafePath } from '../utils/safePath'

/** Same file, two platforms. `[label, posix, windows]`. */
const PAIRS: Array<[string, string, string]> = [
  ['an .aws credentials file', '/home/dev/.aws/credentials', 'C:\\Users\\dev\\.aws\\credentials'],
  ['an .ssh private key', '/home/dev/.ssh/id_rsa', 'C:\\Users\\dev\\.ssh\\id_rsa'],
  ['a project .env', '/home/dev/proj/.env', 'C:\\Users\\dev\\proj\\.env'],
  ['a suffixed .env.local', '/home/dev/proj/.env.local', 'C:\\Users\\dev\\proj\\.env.local'],
  ['the .git directory', '/home/dev/proj/.git/config', 'C:\\Users\\dev\\proj\\.git\\config'],
  ['a .kube config', '/home/dev/.kube/config', 'C:\\Users\\dev\\.kube\\config'],
  ['a .docker config', '/home/dev/.docker/config.json', 'C:\\Users\\dev\\.docker\\config.json'],
]

describe('isSafePath', () => {
  it('refuses each sensitive location on POSIX — the behaviour being preserved', () => {
    for (const [label, posix] of PAIRS) {
      expect(isSafePath(posix), `${label} (posix)`).toBe(false)
    }
  })

  it('refuses the same locations written with Windows separators', () => {
    for (const [label, , windows] of PAIRS) {
      expect(isSafePath(windows), `${label} (windows)`).toBe(false)
    }
  })

  it('refuses a parent traversal on both platforms', () => {
    expect(isSafePath('/home/dev/../../etc/passwd')).toBe(false)
    expect(isSafePath('C:\\Users\\dev\\..\\..\\windows\\win.ini')).toBe(false)
    // A UNC path is still a path: the leading `\\` must not be read as an empty
    // segment that somehow excuses the rest.
    expect(isSafePath('\\\\share\\team\\.ssh\\id_rsa')).toBe(false)
  })

  it('still allows ordinary source files on both platforms', () => {
    expect(isSafePath('/home/dev/proj/src/main.ts')).toBe(true)
    expect(isSafePath('C:\\Users\\dev\\proj\\src\\main.ts')).toBe(true)
    expect(isSafePath('src/components/App.tsx')).toBe(true)
  })

  /**
   * The guard is segment-EQUALITY plus a `name.` prefix rule, not a substring
   * match, and these pin that it stays so. Without the distinction a
   * normalizing fix is free to over-refuse: `.environment` and `.gitignore`
   * both contain a sensitive name, and `my.env` ends with one.
   *
   * `.envrc` is the case the existing DiffBlock suite already pins on POSIX
   * ("allows paths that merely start with a sensitive name"); it is repeated
   * here in its Windows spelling so a separator fix cannot quietly change it.
   */
  it('does not over-refuse a name that merely contains a sensitive one', () => {
    expect(isSafePath('/home/dev/proj/.envrc')).toBe(true)
    expect(isSafePath('C:\\Users\\dev\\proj\\.envrc')).toBe(true)
    expect(isSafePath('C:\\Users\\dev\\proj\\.environment')).toBe(true)
    expect(isSafePath('C:\\Users\\dev\\proj\\.gitignore')).toBe(true)
    expect(isSafePath('C:\\Users\\dev\\proj\\my.env')).toBe(true)
    // `..` is refused; a file literally named `...` is not a traversal.
    expect(isSafePath('C:\\Users\\dev\\proj\\...')).toBe(true)
  })
})
