import { readFileSync } from 'node:fs'
import path from 'node:path'

import { render, screen, fireEvent } from '@testing-library/react'

import PromptForm, {
  PROMPT_FILENAME_MAX_BYTES,
  promptNameProblem,
  sanitizePromptName,
  type PromptFormData,
} from '../components/PromptForm'

const EMPTY: PromptFormData = { name: '', description: '', scope: 'global', body: '' }

/** Names with no character in `[a-z0-9-]`, one per script the issue names.
 *
 *  Written as code-point escapes because the repo forbids CJK literals in
 *  source. That is safe HERE in a way it would not be in a locale catalog: the
 *  sanitizer only cares that no character is in the allowed set, so a
 *  mistyped code point still exercises the same branch, whereas a mistyped
 *  translation ships a non-word. */
const NON_LATIN_NAMES: Record<string, string> = {
  japanese: '\u30d7\u30ed\u30f3\u30d7\u30c8',
  hindi: '\u0928\u092e\u0938\u094d\u0924\u0947',
  bengali: '\u09aa\u09cd\u09b0\u09ae\u09cd\u09aa\u099f',
  korean: '\ud504\ub86c\ud504\ud2b8',
  chinese: '\u63d0\u793a\u8bcd',
}

/** Render the create-shaped form (identity fields visible) and return a setter
 *  for the Name field, so each case reads as "type this, assert the hint". */
function renderForm() {
  let data = EMPTY
  const { rerender } = render(<PromptForm data={data} onChange={d => { data = d; rerender(<PromptForm data={d} onChange={() => {}} />) }} />)
  return (name: string) =>
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: name } })
}

describe('sanitizePromptName', () => {
  // The server's own rule, mirrored: lowercase, then every character outside
  // [a-z0-9-] becomes a hyphen, then edge hyphens are stripped.
  it.each([
    ['My Prompt', 'my-prompt'],
    ['My Prompt!', 'my-prompt'],
    ['ALLCAPS', 'allcaps'],
    ['already-fine', 'already-fine'],
    ['keeps9digits', 'keeps9digits'],
    ['  padded  ', 'padded'],
    ['---leading-and-trailing---', 'leading-and-trailing'],
  ])('reduces %j to %j', (raw, want) => {
    expect(sanitizePromptName(raw)).toBe(want)
  })

  // The case the issue is about: a name with nothing in the allowed set leaves
  // no filename at all, which is what the server answers 400 invalid_name for.
  it.each(Object.keys(NON_LATIN_NAMES))('leaves nothing to save for a %s name', script => {
    expect(sanitizePromptName(NON_LATIN_NAMES[script])).toBe('')
  })

  it.each([['!!!'], ['   '], ['-'], ['---'], ['']])('leaves nothing to save for %j', raw => {
    expect(sanitizePromptName(raw)).toBe('')
  })

  // Interior punctuation becomes ONE hyphen per character, exactly as the
  // server's re.sub does -- it never collapses runs.
  it('does not collapse a run of replaced characters', () => {
    expect(sanitizePromptName('a  b')).toBe('a--b')
  })

  // The `u` flag is what makes this true. Matching UTF-16 code units instead
  // would write two hyphens for one astral character and the preview would
  // disagree with the saved filename.
  it('counts an astral character as one replacement, as the server does', () => {
    const astral = String.fromCodePoint(0x1f389) // one code point, two UTF-16 units
    expect(astral).toHaveLength(2)
    expect(sanitizePromptName(`a${astral}b`)).toBe('a-b')
  })

  /* ── Drift guard ────────────────────────────────────────────────────────
   *
   *  Every case above restates the Python rule in TypeScript, which pins the
   *  mirror to what we BELIEVE the server does. That is the wrong half to pin
   *  alone: if the handler's expression changes, all of them still pass and the
   *  preview goes on confidently showing a filename the server will not use --
   *  green and wrong, which is worse than having no preview.
   *
   *  So the source expression itself is pinned here, next to the mirror that
   *  has to follow it. Reading backend source from a website test is the
   *  established shape for this (`src/apps/mochi/test/mochiVocabularyLabels.test.ts`
   *  reads the mochi package the same way).
   *
   *  A failure here does NOT mean this file is wrong: it means the server rule
   *  moved and `sanitizePromptName` has to move with it. */
  it('still mirrors the expression the create handler actually runs', () => {
    // `__dirname`, not `import.meta.url`: under vitest the module URL is an
    // http:// one, so fileURLToPath refuses it. This is the shape the mochi
    // test uses for the same reason.
    const handler = path.resolve(
      __dirname,
      '../../../src/kiro_crew/dashboard/handlers/prompts.py',
    )
    const source = readFileSync(handler, 'utf-8')
    expect(source).toContain(
      'safe_name = re.sub(r"[^a-z0-9\\-]", "-", raw_name.lower()).strip("-")',
    )
    // Same reasoning for the cap: a client that disagrees with the server about
    // the number predicts the wrong refusal.
    expect(source).toContain(`MAX_PROMPT_NAME_BYTES = ${PROMPT_FILENAME_MAX_BYTES}`)
    expect(source).toContain('len(f"{safe_name}.md".encode("utf-8")) > MAX_PROMPT_NAME_BYTES')
  })
})

describe('promptNameProblem', () => {
  it('reports nothing for an empty field, which is not yet a problem', () => {
    expect(promptNameProblem('')).toBeNull()
    expect(promptNameProblem('   ')).toBeNull()
  })

  it('reports no-stem when nothing survives sanitizing', () => {
    expect(promptNameProblem(NON_LATIN_NAMES.japanese)).toBe('no-stem')
    expect(promptNameProblem('!!!')).toBe('no-stem')
  })

  it('accepts a name whose filename is exactly at the byte cap', () => {
    // stem + '.md' == the cap, so the handler's `> cap` does not fire.
    const stem = 'a'.repeat(PROMPT_FILENAME_MAX_BYTES - '.md'.length)
    expect(promptNameProblem(stem)).toBeNull()
  })

  it('reports too-long one byte over the cap', () => {
    const stem = 'a'.repeat(PROMPT_FILENAME_MAX_BYTES - '.md'.length + 1)
    expect(promptNameProblem(stem)).toBe('too-long')
  })

  /* Sanitizing flattens every character to `[a-z0-9-]` BEFORE the cap is
   * applied, so a long non-Latin name reaches the cap as a long run of hyphens
   * rather than by multi-byte expansion -- the stem is always ASCII and its
   * byte length always equals its character length. Pinned because the
   * intuition runs the other way, and because `promptNameProblem` encodes to
   * bytes: this is the case that shows encoding is about agreeing with the
   * server's own count, not about a divergence that exists today. */
  it('measures the sanitized stem, so a long non-Latin name is a long run of hyphens', () => {
    const cyrillic = '\u0434'.repeat(PROMPT_FILENAME_MAX_BYTES)
    const stem = sanitizePromptName(`a${cyrillic}a`)
    expect(stem).toMatch(/^a-+a$/)
    expect(new TextEncoder().encode(stem).length).toBe(stem.length)
    expect(promptNameProblem(`a${cyrillic}a`)).toBe('too-long')
  })
})

describe('PromptForm name hint', () => {
  it('states the rule generically before anything is typed', () => {
    render(<PromptForm data={EMPTY} onChange={() => {}} />)
    // The literal placeholder, rendered through the SAME string the preview
    // uses -- there is no second catalog entry for the empty state.
    expect(screen.getByText(/Saved as <name>\.md/)).toBeInTheDocument()
  })

  it('previews the sanitized filename as the user types', () => {
    const type = renderForm()
    type('My Prompt!')
    expect(screen.getByText(/Saved as my-prompt\.md/)).toBeInTheDocument()
  })

  it('says why a name that sanitizes away cannot be saved', () => {
    const type = renderForm()
    type(NON_LATIN_NAMES.japanese)
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()
    // Not the preview: there is no filename to preview.
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })

  it('names the byte cap when the filename would be too long', () => {
    const type = renderForm()
    type('a'.repeat(PROMPT_FILENAME_MAX_BYTES))
    expect(screen.getByText(new RegExp(`at most ${PROMPT_FILENAME_MAX_BYTES} bytes`))).toBeInTheDocument()
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })

  it('describes the Name input with the hint, so the filename is not sighted-only', () => {
    const type = renderForm()
    type('My Prompt')
    const input = screen.getByPlaceholderText('my-prompt-name')
    const hintId = input.getAttribute('aria-describedby')
    expect(hintId).toBeTruthy()
    expect(document.getElementById(hintId as string)).toHaveTextContent(/Saved as my-prompt\.md/)
  })

  it('hides the name field entirely when editing an existing prompt', () => {
    render(<PromptForm data={{ ...EMPTY, name: 'fixed' }} onChange={() => {}} hideIdentity />)
    expect(screen.queryByPlaceholderText('my-prompt-name')).not.toBeInTheDocument()
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })
})
