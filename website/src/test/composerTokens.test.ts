import { describe, it, expect } from 'vitest'
import { matchFileToken, matchSkillToken, replaceTokenAtCaret } from '../components/composerTokens'

// These matchers take the text BEFORE the caret. A returned string is the query
// (the picker opens); null means the caret is not inside a token (picker closed).

describe('matchSkillToken (caret-relative $ detection)', () => {
  it('opens on a bare $ at start or after whitespace (full list)', () => {
    expect(matchSkillToken('$')).toBe('')
    expect(matchSkillToken('hello $')).toBe('')
  })

  it('captures the slug up to the caret', () => {
    expect(matchSkillToken('$cr')).toBe('cr')
    expect(matchSkillToken('use $browser-auth')).toBe('browser-auth')
    expect(matchSkillToken('$oncall/handover')).toBe('oncall/handover')
  })

  it('fires mid-sentence: caret sits right after the token, text follows AFTER the caret', () => {
    // The component passes value.slice(0, caret); trailing text is not in `before`.
    // "check this $cr| more" -> before = "check this $cr"
    expect(matchSkillToken('check this $cr')).toBe('cr')
  })

  it('does NOT fire when the $ token is not the token at the caret', () => {
    // "$cr more|" -> before = "$cr more"; caret is after "more", not the token.
    expect(matchSkillToken('$cr more')).toBeNull()
    // whitespace immediately after $ breaks the token
    expect(matchSkillToken('$ ')).toBeNull()
  })

  it('excludes uppercase-led and mid-word shapes ($PATH, $HOME, foo$bar)', () => {
    expect(matchSkillToken('$PATH')).toBeNull()      // uppercase-led (not in [a-z0-9])
    expect(matchSkillToken('echo $HOME')).toBeNull()
    expect(matchSkillToken('foo$bar')).toBeNull()     // $ not at a word boundary
  })

  it('matches digit-led tokens — backend $skill grammar is [a-z0-9]-led', () => {
    // _DOLLAR_SKILL_PATTERN in skills.py allows a leading digit; the picker
    // stays in parity (an empty match list simply shows nothing in the UI).
    expect(matchSkillToken('$5')).toBe('5')
    expect(matchSkillToken('$3d-tool')).toBe('3d-tool')
    expect(matchSkillToken('it costs $5')).toBe('5')
  })
})

describe('matchFileToken (caret-relative @ detection)', () => {
  it('opens on a bare @ and captures the query up to the caret', () => {
    expect(matchFileToken('@')).toBe('')
    expect(matchFileToken('open @src/App')).toBe('src/App')
  })

  it('fires mid-sentence with text after the caret excluded from `before`', () => {
    // "see @src/main.ts| for details" -> before = "see @src/main.ts"
    expect(matchFileToken('see @src/main.ts')).toBe('src/main.ts')
  })

  it('does NOT fire when the caret is past the token or @ is mid-word', () => {
    expect(matchFileToken('@src/App and more')).toBeNull() // whitespace after token
    expect(matchFileToken('foo@bar')).toBeNull()           // @ not at a boundary
  })
})

describe('replaceTokenAtCaret (caret-relative insertion)', () => {
  it('replaces the $token at the caret and preserves text after the caret', () => {
    // "check this $cr| more" -> caret after "$cr" (index 14)
    const value = 'check this $cr more'
    const caret = 'check this $cr'.length
    const next = replaceTokenAtCaret(value, caret, /(^|[\s])\$[a-z0-9/_-]*$/, '$cr-review ')
    expect(next.value).toBe('check this $cr-review  more')
    expect(next.caret).toBe('check this $cr-review '.length)
  })

  it('replaces the @token at the caret and preserves text after the caret', () => {
    const value = 'see @src/ma for details'
    const caret = 'see @src/ma'.length
    const next = replaceTokenAtCaret(value, caret, /(^|[\s])@\S*$/, '@src/main.ts ')
    expect(next.value).toBe('see @src/main.ts  for details')
    expect(next.caret).toBe('see @src/main.ts '.length)
  })

  it('preserves the word-boundary prefix (leading space) before the token', () => {
    const next = replaceTokenAtCaret('a $c', 4, /(^|[\s])\$[a-z0-9/_-]*$/, '$cr-review ')
    expect(next.value).toBe('a $cr-review ')
  })
})

// The headline motivation is "token followed by a newline / trailing text no
// longer closes the picker" — because the component slices to the caret. These
// lock the multi-line case and the detection↔insertion span agreement.
const SKILL_INSERT_RE = /(^|[\s])\$[a-z0-9/_-]*$/
const FILE_INSERT_RE = /(^|[\s])@\S*$/

describe('caret-relative detection across newlines + detection↔insertion agreement', () => {
  it('opens on a $ token at the caret on a later line (newline in before-slice)', () => {
    expect(matchSkillToken('line one\n$cr')).toBe('cr')
    expect(matchFileToken('line one\n@src/a')).toBe('src/a')
  })

  it('opens on a bare $ right after a newline (full list)', () => {
    expect(matchSkillToken('first line\n$')).toBe('')
  })

  it('insertion replaces exactly the span detection matched (no drift on the real path)', () => {
    // "hi $cr| more\nnext" — before-caret is "hi $cr". detection sees "cr";
    // insertion (permissive regex) replaces the identical $cr span, leaving the
    // after-caret text (including the trailing newline/next line) untouched.
    const value = 'hi $cr more\nnext'
    const caret = 'hi $cr'.length
    expect(matchSkillToken(value.slice(0, caret))).toBe('cr')
    const next = replaceTokenAtCaret(value, caret, SKILL_INSERT_RE, '$cr-review ')
    expect(next.value).toBe('hi $cr-review  more\nnext')
  })

  it('@ insertion preserves after-caret content across a newline', () => {
    const value = 'see @src/ma\nrest'
    const caret = 'see @src/ma'.length
    expect(matchFileToken(value.slice(0, caret))).toBe('src/ma')
    const next = replaceTokenAtCaret(value, caret, FILE_INSERT_RE, '@src/main.ts ')
    expect(next.value).toBe('see @src/main.ts \nrest')
  })
})
