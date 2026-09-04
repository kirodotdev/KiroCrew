import { readFileSync } from 'node:fs'
import path from 'node:path'

import { render, screen, fireEvent } from '@testing-library/react'

import SkillForm, {
  sanitizeSkillName,
  skillNameProblem,
  skillPathProblem,
  skillPostPath,
  type SkillFormData,
} from '../components/SkillForm'

const EMPTY: SkillFormData = {
  name: '',
  category: '',
  description: '',
  triggers: '',
  tags: '',
  always: false,
  body: '',
}

/** Names with no character in `[a-z0-9-/]`, one per script the issue names.
 *
 *  Written as code-point escapes because the repo forbids CJK literals in
 *  source. That is safe HERE in a way it would not be in a locale catalog: the
 *  sanitizer only cares that no character is in the allowed set, so a mistyped
 *  code point still exercises the same branch, whereas a mistyped translation
 *  ships a non-word. */
const NON_LATIN_NAMES: Record<string, string> = {
  japanese: '\u30b9\u30ad\u30eb',
  hindi: '\u0915\u094c\u0936\u0932',
  bengali: '\u09a6\u0995\u09cd\u09b7\u09a4\u09be',
  korean: '\uc2a4\ud0ac',
  chinese: '\u6280\u80fd',
}

/** Render the create-shaped form (identity fields visible) and return setters
 *  for the Name and Category fields, so each case reads as "type this, assert
 *  the hint". */
function renderForm() {
  let data = EMPTY
  const onChange = (d: SkillFormData) => {
    data = d
    rerender(<SkillForm data={d} onChange={onChange} />)
  }
  const { rerender } = render(<SkillForm data={data} onChange={onChange} />)
  return {
    typeName: (name: string) =>
      fireEvent.change(screen.getByPlaceholderText('e.g. my-tool'), { target: { value: name } }),
    typeCategory: (cat: string) =>
      fireEvent.change(screen.getByPlaceholderText('e.g. utils, code'), { target: { value: cat } }),
  }
}

describe('sanitizeSkillName', () => {
  // The server's own rule, mirrored: lowercase, then every character outside
  // [a-z0-9-/] becomes a hyphen, then edge hyphens THEN edge slashes are
  // stripped, then slash runs collapse to a single separator.
  it.each([
    ['My Skill', 'my-skill'],
    ['My Skill!', 'my-skill'],
    ['ALLCAPS', 'allcaps'],
    ['already-fine', 'already-fine'],
    ['keeps9digits', 'keeps9digits'],
    // '/' is preserved for nesting -- the load-bearing difference from prompts.
    ['utils/code', 'utils/code'],
    ['a/b/c', 'a/b/c'],
    // Slash runs collapse to one.
    ['a//b', 'a/b'],
    ['a///b', 'a/b'],
    // Leading/trailing slashes are stripped.
    ['/a/', 'a'],
    ['///a///', 'a'],
    // Leading/trailing hyphens are stripped too.
    ['---leading-and-trailing---', 'leading-and-trailing'],
  ])('reduces %j to %j', (raw, want) => {
    expect(sanitizeSkillName(raw)).toBe(want)
  })

  // The case the issue is about: a name with nothing in the allowed set leaves
  // no filename at all, which is what the server answers 400 invalid_name for.
  it.each(Object.keys(NON_LATIN_NAMES))('leaves nothing to save for a %s name', script => {
    expect(sanitizeSkillName(NON_LATIN_NAMES[script])).toBe('')
  })

  it.each([['!!!'], ['   '], ['-'], ['---'], ['/'], ['///'], ['-/'], ['']])(
    'leaves nothing to save for %j',
    raw => {
      expect(sanitizeSkillName(raw)).toBe('')
    },
  )

  // Interior non-slash punctuation becomes ONE hyphen per character, exactly as
  // the server's re.sub does for the hyphen class -- it never collapses hyphen
  // runs, only slash runs.
  it('does not collapse a run of replaced characters into one hyphen', () => {
    expect(sanitizeSkillName('a  b')).toBe('a--b')
  })

  // The `u` flag is what makes this true. Matching UTF-16 code units instead
  // would write two hyphens for one astral character and the preview would
  // disagree with the saved filename.
  it('counts an astral character as one replacement, as the server does', () => {
    const astral = String.fromCodePoint(0x1f389) // one code point, two UTF-16 units
    expect(astral).toHaveLength(2)
    expect(sanitizeSkillName(`a${astral}b`)).toBe('a-b')
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
   *  has to follow it. A failure here does NOT mean this file is wrong: it
   *  means the server rule moved and `sanitizeSkillName` has to move with it. */
  it('still mirrors the expression the create handler actually runs', () => {
    // `__dirname`, not `import.meta.url`: under vitest the module URL is an
    // http:// one, so fileURLToPath refuses it.
    const handler = path.resolve(
      __dirname,
      '../../../src/kiro_crew/dashboard/handlers/prompts.py',
    )
    const source = readFileSync(handler, 'utf-8')
    expect(source).toContain(
      'safe_name = re.sub(r"[^a-z0-9\\-/]", "-", name.lower()).strip("-").strip("/")',
    )
    expect(source).toContain('safe_name = re.sub(r"/+", "/", safe_name)')
    // Containment alone is blind to a rule ADDED after those two lines -- a length
    // cap, a reserved-name check -- which would leave the mirror silently
    // desynced and this test green. Pinning the COUNT makes a third sanitizing
    // statement in the create handler fail here instead.
    const assignments = source
      .slice(source.indexOf('async def api_skills_create'))
      .split('\nasync def ')[0]
      .match(/^\s*safe_name = /gm)
    expect(assignments).toHaveLength(2)
  })
})

describe('skillPostPath', () => {
  // The gate, the preview and the request all read this, so a change here would
  // desync all three at once rather than one of them.
  it('joins category-then-name, and omits the join when there is no category', () => {
    expect(skillPostPath('code', 'utils')).toBe('utils/code')
    expect(skillPostPath('code', '')).toBe('code')
  })
})

describe('skillNameProblem', () => {
  it('reports nothing for an empty field, which is not yet a problem', () => {
    expect(skillNameProblem('')).toBeNull()
    expect(skillNameProblem('   ')).toBeNull()
  })

  it('reports no-stem when nothing survives sanitizing', () => {
    expect(skillNameProblem(NON_LATIN_NAMES.japanese)).toBe('no-stem')
    expect(skillNameProblem('!!!')).toBe('no-stem')
    expect(skillNameProblem('/')).toBe('no-stem')
  })

  it('reports null for a name that keeps a stem, including a nested one', () => {
    expect(skillNameProblem('My Skill!')).toBeNull()
    expect(skillNameProblem('utils/code')).toBeNull()
  })
})

/** The predicate the gate reads. Its whole reason to exist is that the COMBINED
 *  path cannot answer the question: a surviving segment masks a segment that
 *  sanitized away, so `skillNameProblem(combined)` says "fine" for a name the
 *  server is about to discard entirely. Each row below is a state a user can
 *  reach with two ordinary fields, so a regression here is not theoretical. */
describe('skillPathProblem', () => {
  it('reports no-stem for a name that sanitizes away, with or without a category', () => {
    // Without a category the combined value IS the name, so both predicates agree.
    expect(skillPathProblem(NON_LATIN_NAMES.japanese, '')).toBe('no-stem')
    // WITH one they diverge, and this is the divergence the gate exists for:
    // `utils/<non-Latin>` sanitizes to a non-empty `utils`, so a combined check
    // sees no problem and the server stores a skill named `utils` with the name
    // the user typed thrown away.
    expect(sanitizeSkillName(`utils/${NON_LATIN_NAMES.japanese}`)).toBe('utils')
    expect(skillNameProblem(`utils/${NON_LATIN_NAMES.japanese}`)).toBeNull()
    expect(skillPathProblem(NON_LATIN_NAMES.japanese, 'utils')).toBe('no-stem')
  })

  it('reports no-stem for every named script, so no locale is left unguarded', () => {
    for (const name of Object.values(NON_LATIN_NAMES)) {
      expect(skillPathProblem(name, 'utils')).toBe('no-stem')
    }
  })

  it('reports no-stem for a CATEGORY that sanitizes away, which would be dropped', () => {
    // The mirror image: `<non-Latin>/code` sanitizes to a bare `code`, so the
    // skill silently lands at the top level instead of under the category.
    expect(sanitizeSkillName(`${NON_LATIN_NAMES.chinese}/code`)).toBe('code')
    expect(skillPathProblem('code', NON_LATIN_NAMES.chinese)).toBe('no-stem')
  })

  it('stays silent on an unfinished form rather than reddening a blank field', () => {
    expect(skillPathProblem('', '')).toBeNull()
    expect(skillPathProblem('   ', '')).toBeNull()
    // A category alone is not yet a problem either -- the name is simply unfilled.
    expect(skillPathProblem('', 'utils')).toBeNull()
  })

  it('treats a whitespace-only category as absent, matching what the tab POSTs', () => {
    // A blank category sanitizes away on the server too, so the stored name is
    // the same with or without it. Reporting it would block a valid submission.
    expect(skillPathProblem('code', '   ')).toBeNull()
    expect(sanitizeSkillName('   /code')).toBe('code')
  })

  /* The residual of the same class, one level down: BOTH fields may nest, so
     checking each field whole has the identical blind spot. The unit of judgment
     is the segment. */
  it('reports no-stem for a vanishing segment NESTED inside the name field', () => {
    // `utils/<non-Latin>` sanitizes to a non-empty `utils`, so a whole-field check
    // passes and the server stores `utils` with the typed word discarded.
    expect(sanitizeSkillName(`utils/${NON_LATIN_NAMES.japanese}`)).toBe('utils')
    expect(skillNameProblem(`utils/${NON_LATIN_NAMES.japanese}`)).toBeNull()
    expect(skillPathProblem(`utils/${NON_LATIN_NAMES.japanese}`, '')).toBe('no-stem')
  })

  it('reports no-stem wherever in the path the vanishing segment sits', () => {
    // Leading: the handler's `.strip("-")` eats it from the front just as readily.
    expect(sanitizeSkillName(`${NON_LATIN_NAMES.japanese}/utils`)).toBe('utils')
    expect(skillPathProblem(`${NON_LATIN_NAMES.japanese}/utils`, '')).toBe('no-stem')
    // Middle: end-stripping cannot reach it, so it is STORED -- as the unreadable
    // `---`. Nothing is lost as a path, but the user's word is gone all the same,
    // and refusing is the same answer the top-level case already gives.
    expect(sanitizeSkillName(`a/${NON_LATIN_NAMES.japanese}/b`)).toBe('a/---/b')
    expect(skillPathProblem(`a/${NON_LATIN_NAMES.japanese}/b`, '')).toBe('no-stem')
    // Nested under a category, so both fields contribute segments.
    expect(skillPathProblem(`utils/${NON_LATIN_NAMES.japanese}`, 'top')).toBe('no-stem')
    expect(skillPathProblem('code', `${NON_LATIN_NAMES.japanese}/top`)).toBe('no-stem')
  })

  it('reports no-stem for a segment with no representable character', () => {
    // A segment reduced entirely to hyphens is the precondition for vanishing, so
    // it is refused wherever it sits rather than stored as a directory named `-`.
    expect(skillPathProblem('a/-/b', '')).toBe('no-stem')
    expect(skillPathProblem('a/!!!/b', '')).toBe('no-stem')
  })

  it('skips an EMPTY segment, which the handler collapses without losing anything', () => {
    // `a//b` is stored as `a/b`: a normalization, not a loss, and the preview shows
    // it. Reporting it would refuse a submission that works.
    expect(sanitizeSkillName('a//b')).toBe('a/b')
    expect(skillPathProblem('a//b', '')).toBeNull()
    expect(skillPathProblem('b', 'a/')).toBeNull()
  })

  /* ── The residual of SKIPPING a blank segment ───────────────────────────────
   *
   *  Skipping is right (see above) but it is not free, and both rows below reach
   *  the loop with nothing left to object to. They are the same defect as the
   *  non-Latin cases -- the typed name is not what gets stored -- arrived at
   *  through separators instead of through an unrepresentable script. */

  it('reports no-stem for a name that is nothing BUT separators', () => {
    // Every segment is blank, so the loop sees no segment to judge. Alone this is
    // a server 400, which the mirror exists to pre-empt.
    for (const name of ['/', '//', '///', '  /  ', '/ /']) {
      expect(skillPathProblem(name, '')).toBe('no-stem')
    }
  })

  it('reports no-stem for a separator-only name that a CATEGORY would carry', () => {
    // The sharp case: `utils//` sanitizes to a non-empty `utils`, so the surviving
    // category masks a name that contributed nothing and the server stores the
    // skill under the CATEGORY with the Name silently discarded.
    expect(sanitizeSkillName('utils//')).toBe('utils')
    expect(skillNameProblem('utils//')).toBeNull()
    expect(skillPathProblem('/', 'utils')).toBe('no-stem')
    expect(skillPathProblem('//', 'utils')).toBe('no-stem')
    // Blanks instead of nothing between the slashes: stored as `utils/---`, so the
    // name is not dropped but is unreadable. Same verdict.
    expect(sanitizeSkillName('utils/   /  ')).toBe('utils/---')
    expect(skillPathProblem('   /  ', 'utils')).toBe('no-stem')
  })

  it('reports no-stem for a BLANK segment the handler does not collapse away', () => {
    // Only a literally empty segment is collapsed. A segment of blanks becomes
    // hyphens, and end-stripping reaches just one at each end of the path -- so in
    // the middle it is STORED. `a/ /b` and `a/-/b` are the same file, so a
    // predicate that refused one and allowed the other would contradict itself.
    expect(sanitizeSkillName('a/ /b')).toBe('a/-/b')
    expect(sanitizeSkillName('a/-/b')).toBe('a/-/b')
    expect(skillPathProblem('a/ /b', '')).toBe('no-stem')
    expect(skillPathProblem('a/-/b', '')).toBe('no-stem')
    // Two blank category segments: the leading strip absorbs one, the next is kept.
    expect(sanitizeSkillName('   /  /ok')).toBe('--/ok')
    expect(skillPathProblem('ok', '   /  ')).toBe('no-stem')
  })

  it('still allows a blank segment the handler really does drop, at the edges', () => {
    // The over-refusal to avoid: one blank or separator-only CATEGORY is stripped
    // from the front of the path entirely, so the stored name is the same with or
    // without it and refusing would block a submission that works.
    expect(sanitizeSkillName('   /ok')).toBe('ok')
    expect(sanitizeSkillName('//ok')).toBe('ok')
    expect(skillPathProblem('ok', '   ')).toBeNull()
    expect(skillPathProblem('ok', '/')).toBeNull()
    // And a name may still lead or trail with separators.
    expect(skillPathProblem('/a', '')).toBeNull()
    expect(skillPathProblem('a/', '')).toBeNull()
    expect(skillPathProblem('///a///', '')).toBeNull()
  })

  it('reports no-stem for `.` and `..`, which cannot survive as path segments', () => {
    // Not a traversal guard -- the handler replaces `.` with a hyphen, so `..` can
    // never reach the filesystem. It is the same no-stem answer: a segment written
    // only in characters outside the allowed set leaves nothing to store.
    expect(sanitizeSkillName('..')).toBe('')
    expect(skillPathProblem('.', '')).toBe('no-stem')
    expect(skillPathProblem('..', '')).toBe('no-stem')
    expect(skillPathProblem('../../etc', '')).toBe('no-stem')
    expect(skillPathProblem('a/./b', '')).toBe('no-stem')
  })

  it('reports nothing for a very long segment, which the handler does not cap', () => {
    // `skillNameProblem` has no `too-long` branch on purpose: unlike the prompts
    // handler, the skills one has no byte cap, and predicting a refusal the server
    // never makes would block a name that saves.
    expect(skillPathProblem('x'.repeat(300), '')).toBeNull()
  })

  it('reports null for names and categories that both keep a stem', () => {
    expect(skillPathProblem('My Skill!', '')).toBeNull()
    expect(skillPathProblem('My Skill!', 'Utils Code')).toBeNull()
    expect(skillPathProblem('utils/code', '')).toBeNull()
  })
})

describe('SkillForm name hint', () => {
  it('states the rule generically before anything is typed', () => {
    render(<SkillForm data={EMPTY} onChange={() => {}} />)
    // The literal placeholder, rendered through the SAME string the preview
    // uses -- there is no second catalog entry for the empty state.
    expect(screen.getByText(/Saved as <name>/)).toBeInTheDocument()
  })

  it('previews the sanitized filename as the user types', () => {
    const { typeName } = renderForm()
    typeName('My Skill!')
    expect(screen.getByText(/Saved as my-skill/)).toBeInTheDocument()
  })

  it('previews the COMBINED category/name, as the server sanitizes it', () => {
    const { typeName, typeCategory } = renderForm()
    typeName('My Skill')
    typeCategory('Utils Code')
    // category-then-name, both sanitized and joined by the surviving slash.
    expect(screen.getByText(/Saved as utils-code\/my-skill/)).toBeInTheDocument()
  })

  it('says why a name that sanitizes away cannot be saved', () => {
    const { typeName } = renderForm()
    typeName(NON_LATIN_NAMES.japanese)
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()
    // Not the preview: there is no filename to preview.
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })

  it('describes the Name input with the hint, so the filename is not sighted-only', () => {
    const { typeName } = renderForm()
    typeName('My Skill')
    const input = screen.getByPlaceholderText('e.g. my-tool')
    const hintId = input.getAttribute('aria-describedby')
    expect(hintId).toBeTruthy()
    expect(document.getElementById(hintId as string)).toHaveTextContent(/Saved as my-skill/)
  })

  it('reddens the hint for a vanishing name even when a category survives', () => {
    const { typeName, typeCategory } = renderForm()
    typeCategory('utils')
    typeName(NON_LATIN_NAMES.japanese)
    // Previewing `Saved as utils` here would be a lie about a name the user never
    // typed, and it is the frame the Create gate has to agree with.
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })

  it('reddens the hint for a vanishing CATEGORY, which would be silently dropped', () => {
    const { typeName, typeCategory } = renderForm()
    typeName('code')
    typeCategory(NON_LATIN_NAMES.chinese)
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()
  })

  it('promises no filename for a category typed before any name', () => {
    const { typeCategory } = renderForm()
    typeCategory('utils')
    // The combined value is a non-empty `utils/`, so keying "has the user named
    // this?" off it would advertise `Saved as utils` for an unnamed skill.
    expect(screen.getByText(/Saved as <name>/)).toBeInTheDocument()
  })

  it('reddens the hint for a separator-only name under a surviving category', () => {
    const { typeName, typeCategory } = renderForm()
    typeCategory('utils')
    typeName('/')
    // `Saved as utils` is the lie to avoid: `utils` is the category, and the Name
    // field contributed nothing the server would store.
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })

  it('promises no filename for a whitespace-only name', () => {
    const { typeName } = renderForm()
    typeName('   ')
    // A truthy string, but not a name: the server strips it and refuses with
    // `name_required`, so the field must not claim a filename for it.
    expect(screen.getByText(/Saved as <name>/)).toBeInTheDocument()
  })

  it('hides the name field entirely when editing an existing skill', () => {
    render(<SkillForm data={{ ...EMPTY, name: 'fixed' }} onChange={() => {}} hideIdentity />)
    expect(screen.queryByPlaceholderText('e.g. my-tool')).not.toBeInTheDocument()
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })
})
