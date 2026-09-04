import { describe, expect, it } from 'vitest'

import {
  assembleSkillContent,
  canEditStructured,
  commentIsTheOnlyObstacle,
  frontmatterError,
  parseFrontmatter,
  parseSkillContent,
} from '../components/SkillForm'

/**
 * The structured skill editor owns five fields and must leave every other byte of
 * the frontmatter alone. It does that by parsing the block with a real YAML
 * parser, replacing the SOURCE RANGE of each field it owns, and copying the rest
 * through — so what counts as a key, a value's continuation or a comment is
 * decided by the grammar rather than by matching line shapes.
 *
 * Two runtime keys depend on that guarantee: `repo_scope` (the
 * matcher's repo guard) and `inject_on_trigger` (the full-body opt-out).
 */
describe('structured editor preserves unmodelled frontmatter', () => {
  const RAW = [
    '---',
    'name: worktree-dev',
    'description: Develop in a worktree',
    'triggers: worktree, build gate',
    'repo_scope: src/kiro_crew',
    'inject_on_trigger: false',
    '---',
    '',
    '# Body',
    'Steps here.',
  ].join('\n')

  it('round-trips repo_scope, which gates where the skill may match', () => {
    const out = assembleSkillContent(parseSkillContent(RAW, 'kirocrew-dev/worktree-dev'))
    expect(parseFrontmatter(out).meta.repo_scope).toBe('src/kiro_crew')
  })

  it('round-trips inject_on_trigger, so editing a description cannot re-enable injection', () => {
    const out = assembleSkillContent(parseSkillContent(RAW, 'kirocrew-dev/worktree-dev'))
    expect(parseFrontmatter(out).meta.inject_on_trigger).toBe('false')
  })

  it('still writes the keys the form owns', () => {
    const data = parseSkillContent(RAW, 'kirocrew-dev/worktree-dev')
    const meta = parseFrontmatter(assembleSkillContent({ ...data, description: 'Changed' })).meta
    expect(meta.name).toBe('worktree-dev')
    expect(meta.description).toBe('Changed')
    expect(meta.triggers).toBe('worktree, build gate')
  })

  it('writes a managed key exactly once even when the file declares it twice', () => {
    /* A duplicate key is invalid YAML, so the block is not spliceable and the
       editor hands it back raw rather than picking one of the two to rewrite. */
    const raw = ['---', 'name: first', 'name: second', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 's')
    expect(data.raw).toBe(raw)
    expect(data.frontmatter).toBeUndefined()
    expect(assembleSkillContent(data)).toBe(raw)
  })

  it('keeps a managed field byte-identical when its value was not edited', () => {
    /* The form's five fields are re-rendered only when their value actually
       changed, so an untouched `description: |` block keeps its own style. */
    const raw = ['---', 'name: s', 'description: |', '  one', '  two', 'tags:', '  - a', '  - b', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('description: |\n  one\n  two')
    expect(out).toContain('tags:\n  - a\n  - b')
  })

  /**
   * The form has no idea what YAML type an unmodelled field is, so it must copy
   * the ORIGINAL bytes rather than reserialize a parsed value. Reserializing
   * would turn a list or a nested map into a literal string, and re-fold a `>`
   * scalar — the field would survive in shape but not in text, which is worse
   * than losing it loudly.
   */
  it('preserves a YAML list as a list, not as a block scalar', () => {
    const raw = ['---', 'name: s', 'mcp_servers:', '  - alpha', '  - beta', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('mcp_servers:\n  - alpha\n  - beta')
    expect(out).not.toContain('mcp_servers: |')
  })

  it('preserves a nested map with its indentation and keys', () => {
    const raw = ['---', 'name: s', 'limits:', '  cpu: 2', '  memory: 4G', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('limits:\n  cpu: 2\n  memory: 4G')
  })

  it('keeps a folded scalar folded instead of retyping it as literal', () => {
    const raw = ['---', 'name: s', 'note: >', '  one', '  two', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('note: >\n  one\n  two')
    expect(out).not.toContain('note: |')
  })

  it('keeps a blank line inside a multiline value', () => {
    /* A paragraph break inside a block scalar is content. Dropping it rewrites
       the author's text while they were editing something else. */
    const raw = ['---', 'name: s', 'note: |', '  one', '', '  two', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('note: |\n  one\n\n  two')
  })

  it('neither invents nor deletes a blank line before the closing fence', () => {
    /* The line-based walk dropped a blank line that sat before `---`, rewriting
       the author's block during an unrelated edit. Copying bytes keeps it. With
       `|` clip chomping it is not part of the value either way, so this is purely
       about not touching what the form does not own. */
    const withBlank = ['---', 'name: s', 'note: |', '  one', '', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(withBlank, 's'))
    expect(out).toContain('note: |\n  one\n\n---')
    // Stable across repeated saves: the blank is preserved, never multiplied.
    expect(assembleSkillContent(parseSkillContent(out, 's'))).toBe(out)

    const withoutBlank = ['---', 'name: s', 'note: |', '  one', '---', '', '# Body'].join('\n')
    expect(assembleSkillContent(parseSkillContent(withoutBlank, 's'))).toContain('note: |\n  one\n---')
  })

  it('preserves an indentless list, which YAML allows under a key', () => {
    const raw = ['---', 'name: s', 'custom:', '- alpha', '- beta', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('custom:\n- alpha\n- beta')
  })

  it('preserves a comment line inside an unmodelled field', () => {
    const raw = ['---', 'name: s', 'custom:', '  # why', '  - alpha', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('custom:\n  # why\n  - alpha')
  })

  /**
   * The invariant, stated once instead of one test per YAML shape: every field
   * the form does not model comes back out byte-identical. Four review rounds
   * were spent adding shapes to an accept-list; this asserts the property that
   * makes the list unnecessary.
   */
  it('re-emits every unmodelled field byte-identically', () => {
    const blocks = [
      'repo_scope: src/kiro_crew',
      'inject_on_trigger: false',
      'indentless:\n- alpha\n- beta',
      'indented:\n  - one\n  - two',
      'nested:\n  cpu: 2\n  memory: 4G',
      'folded: >\n  soft\n  wrapped',
      'literal: |\n  para one\n\n  para two',
      'commented:\n  # why this exists\n  - value',
      'quoted-key-holder:\n  "dotted.inner": v',
      'empty-value:',
      "single: 'quoted value'",
      'trailing: value # with a trailing comment',
    ]
    const raw = ['---', 'name: s', 'description: d', ...blocks, '---', '', '# Body'].join('\n')

    // Edit a modelled field, which is the operation that used to destroy them.
    const data = parseSkillContent(raw, 's')
    const out = assembleSkillContent({ ...data, description: 'edited' })

    for (const block of blocks) expect(out).toContain(block)
    expect(parseFrontmatter(out).meta.description).toBe('edited')
  })

  /* The residual case #1825 was filed for: a top-level line that is not a
     recognized `key:` and follows a MODELLED key. The line-based walk could only
     attach it to that key, so re-emitting the key from form state destroyed it.
     A parser puts it outside every key's range, so it is copied where it
     stands. */
  it('keeps a comment that follows a modelled key when that key is rewritten', () => {
    const raw = ['---', 'name: s', 'always: true', '# why it is pinned', 'triggers: t', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 's')
    expect(data.always).toBe(true)

    const out = assembleSkillContent({ ...data, triggers: 'changed' })
    expect(out).toContain('# why it is pinned')
    expect(parseFrontmatter(out).meta.always).toBe('true')
    expect(parseFrontmatter(out).meta.triggers).toBe('changed')
  })

  it('keeps a quoted key that follows a modelled key', () => {
    const raw = ['---', 'name: s', 'description: d', '"my.key": v', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('"my.key": v')
  })

  it('keeps a dotted key that follows a modelled key', () => {
    const raw = ['---', 'name: s', 'description: d', 'dotted.key: w', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('dotted.key: w')
  })

  it('keeps a comment that precedes a modelled key when that key is rewritten', () => {
    const raw = ['---', 'name: s', '# explains the flag', 'always: true', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), name: 'renamed' })
    expect(out).toContain('# explains the flag\nalways: true')
    expect(parseFrontmatter(out).meta.name).toBe('renamed')
  })

  it('keeps a comment that closes the block', () => {
    const raw = ['---', 'name: s', 'description: d', '# a closing note', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('# a closing note\n---')
  })

  it('does not let a top-level comment corrupt an unmodelled scalar', () => {
    const raw = ['---', 'name: s', 'repo_scope: src/kiro_crew', '# scoped on purpose', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(parseFrontmatter(out).meta.repo_scope).toBe('src/kiro_crew')
    expect(out).toContain('repo_scope: src/kiro_crew\n# scoped on purpose')
  })

  it('removes a managed key without leaving a blank line behind', () => {
    const raw = ['---', 'name: s', 'always: true', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), always: false })
    expect(out).toContain('name: s\nrepo_scope: x\n---')
    expect(out).not.toContain('always:')
  })

  it('appends a managed field the file never declared, in the form order', () => {
    const raw = ['---', 'name: s', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), tags: 'a, b' })
    expect(out).toContain('name: s\nrepo_scope: x\ntags: [a, b]')
  })

  /* GPT 5.6 review, head a1f80c728: a BLOCK value's source range ends PAST its
     terminating newline, unlike a plain scalar's. Rewriting such a field used to
     swallow that newline, so the next key was glued onto the new value; dropping
     the field took the following line with it. Both are silent corruption of the
     saved skill, and no earlier test edited a MULTILINE managed value. */
  it('does not glue the next key onto an edited block-scalar value', () => {
    const raw = ['---', 'name: s', 'description: |', '  one', '  two', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('description: edited\nrepo_scope: x')
    expect(parseFrontmatter(out).meta.repo_scope).toBe('x')
  })

  it('hands a folded value to the raw editor rather than restructuring it', () => {
    /* Was: an edited folded value spliced without gluing the next key on. That still
       holds for LITERAL block scalars, but a FOLDED one is now refused. The reader folds
       single breaks to spaces, preserves blank-line runs as newlines, and keeps breaks
       next to more-indented lines; reproducing those rules in TypeScript to verify
       agreement is the cross-language coupling this change exists to avoid, and three
       attempts at predicting agreement from the indicator alone were each wrong. So the
       block opens raw, where nothing is rewritten and the next key cannot be glued on.
       A deliberate narrowing of #7003's behaviour: a folded description is no longer
       structurally editable, traded for the guarantee that no save redefines the file. */
    const raw = ['---', 'name: s', 'description: >', '  soft', '  wrapped', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(parseSkillContent(raw, 's').raw).toBeDefined()
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('does not glue the next key onto an edited block-sequence value', () => {
    const raw = ['---', 'name: s', 'tags:', '  - a', '  - b', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), tags: 'c, d' })
    expect(out).toContain('tags: [c, d]\nrepo_scope: x')
    expect(parseFrontmatter(out).meta.repo_scope).toBe('x')
  })

  it('does not delete the following line when a block-sequence field is cleared', () => {
    const raw = ['---', 'name: s', 'tags:', '  - a', '  - b', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), tags: '' })
    expect(out).toContain('name: s\nrepo_scope: x\n---')
    expect(out).not.toContain('tags')
    expect(parseFrontmatter(out).meta.repo_scope).toBe('x')
  })

  it('keeps a multiline managed field byte-identical when it is not edited', () => {
    const raw = ['---', 'name: s', 'description: |', '  one', '  two', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('description: |\n  one\n  two\nrepo_scope: x')
  })

  /* GPT 5.6 review, head 967889b0e. Three ways the splice touched a field the
     user had not edited. All three contradicted this PR's own stated invariant,
     which is why they count as regressions rather than nitpicks. */
  it('keeps an untouched managed field whose value is legitimately empty', () => {
    /* `tags: []` renders as "absent", so asking the writer first deleted a line
       nobody edited. The unchanged check has to run before the drop branch. */
    const raw = ['---', 'name: s', 'tags: []', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('tags: []')
    expect(out).toContain('repo_scope: x')
  })

  it('keeps an untouched bare managed key', () => {
    const raw = ['---', 'name: s', 'triggers:', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('triggers:\n')
    expect(out).toContain('repo_scope: x')
  })

  it('keeps an untouched always whose value is not true, whatever it spells', () => {
    /* GPT 5.6 review, head ba189b79b. The first fix here enumerated `''` and
       `'false'`, which missed every other non-`true` spelling: `yes`/`no` parse as
       STRINGS under the YAML 1.2 core schema and `0` as a number, so each was
       deleted on an unrelated edit. The rule is now the inversion of how the form
       reads the flag, so no spelling has to be listed. */
    for (const decl of ['always: false', 'always: yes', 'always: no', 'always: 0', 'always: off', 'always: FALSE']) {
      const raw = ['---', 'name: s', decl, 'repo_scope: x', '---', '', '# Body'].join('\n')
      const data = parseSkillContent(raw, 's')
      expect(data.always, decl).toBe(false)
      const out = assembleSkillContent({ ...data, description: 'edited' })
      expect(out, decl).toContain(decl)
    }
  })

  it('keeps an untouched always: true and drops it only when unchecked', () => {
    const raw = ['---', 'name: s', 'always: true', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 's')
    expect(data.always).toBe(true)
    expect(assembleSkillContent({ ...data, description: 'edited' })).toContain('always: true')
    expect(assembleSkillContent({ ...data, always: false })).not.toContain('always')
  })

  it('writes always: true when the box is checked over a non-true spelling', () => {
    const raw = ['---', 'name: s', 'always: no', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), always: true })
    expect(out).toContain('always: true')
    expect(out).not.toContain('always: no')
  })

  it('still drops always when the user unchecks it', () => {
    const raw = ['---', 'name: s', 'always: true', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), always: false })
    expect(out).not.toContain('always')
    expect(out).toContain('name: s\nrepo_scope: x')
  })

  it('keeps the trailing blank line when it appends a new field', () => {
    const raw = ['---', 'name: s', 'repo_scope: x', '', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), tags: 'a, b' })
    expect(out).toContain('tags: [a, b]\n\n---')
  })

  it('gives a bare managed key a value without corrupting its line', () => {
    /* GPT 5.6 review, head f5a73cc20, claimed the splice keeps the old colon and
       writes `triggers: alpha:`. That is a false positive -- a bare key's value is
       a NULL scalar whose range ends AFTER the colon, so `slice(keyStart, valueEnd)`
       is `"triggers:"` and the colon is replaced with the rest. A bare key with a
       trailing COMMENT is no longer spliced at all (see the raw-editing cases
       below), so it is not among these shapes. */
    const shapes: [string, string[]][] = [
      ['mid-block', ['---', 'name: s', 'triggers:', 'repo_scope: x', '---', '', '# Body']],
      ['last key', ['---', 'name: s', 'repo_scope: x', 'triggers:', '---', '', '# Body']],
      ['trailing space', ['---', 'name: s', 'triggers: ', 'repo_scope: x', '---', '', '# Body']],
    ]
    for (const [label, lines] of shapes) {
      const raw = lines.join('\n')
      const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), triggers: 'alpha' })
      expect(out, label).not.toMatch(/triggers:[^\n]*:/)
      expect(parseFrontmatter(out).meta.triggers, label).toBe('alpha')
      expect(parseFrontmatter(out).meta.repo_scope, label).toBe('x')
    }
  })

  it('does not overwrite a name the file declares as empty', () => {
    /* GPT 5.6 review, head 00abdc19b: `meta.name || name` substituted the
       key-derived name for an explicit `name: ""`, so editing an unrelated field
       rewrote a field the user never touched. */
    for (const decl of ['name: ""', 'name:', "name: ''"]) {
      const raw = ['---', decl, 'repo_scope: x', '---', '', '# Body'].join('\n')
      const data = parseSkillContent(raw, 'derived-key')
      expect(data.name, decl).toBe('')
      const out = assembleSkillContent({ ...data, description: 'edited' })
      expect(out, decl).toContain(decl)
      expect(out, decl).not.toContain('derived-key')
    }
  })

  it('still derives a name when the file declares none', () => {
    const raw = ['---', 'repo_scope: x', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 'kirocrew-dev/derived-key')
    expect(data.name).toBe('derived-key')
    expect(assembleSkillContent(data)).toContain('name: derived-key')
  })

  it('writes managed values in a form the backend reader decodes', () => {
    /* The backend reads frontmatter with its own line parser that strips quote
       characters and does NOT unescape, so the writer must not emit a form that
       needs unescaping. Verified against the real reader; a tab is emitted raw
       and a colon/hash/padded value is quoted but escape-free, so all round-trip.
       See the review thread for the measured table. */
    const cases = ['a\tb', 'ratio: 3', 'a # b', '  padded', 'padded  ', 'one\ntwo', 'a\\b']
    for (const value of cases) {
      const out = assembleSkillContent({
        name: 's', category: '', description: value,
        triggers: '', tags: '', always: false, body: '# Body',
      })
      expect(parseFrontmatter(out).meta.description, JSON.stringify(value)).toBe(value)
      // No backslash escape sequence introduced by the writer.
      expect(out, JSON.stringify(value)).not.toMatch(/description: ".*\\[rntx]/)
    }
  })

  it('does not relocate the newlines a keep-chomped scalar owns', () => {
    /* Under `|+` the trailing blank lines ARE content, so treating them as the gap
       before the closing fence moved them out of the value and behind an appended
       field. */
    const raw = ['---', 'repo_scope: x', 'note: |+', '  text', '', '', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 'derived'), tags: 'a' })
    expect(out).toContain('note: |+\n  text\n\n')
    expect(parseFrontmatter(out).meta.note).toBe('text\n\n')
  })

  it('does not move block-scalar trailing spaces when appending a field', () => {
    const raw = ['---', 'repo_scope: x', 'note: |-', '  text  ', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 'derived')
    const out = assembleSkillContent({ ...data, tags: 'a' })
    expect(out).toContain('note: |-\n  text  ')
  })

  it('does not mistake a hash inside a quoted value for a comment', () => {
    /* A `#` inside a quoted value is content, not a comment, so this block stays
       structurally editable -- the guard keys off the parser's comment, not a
       textual `#`. */
    const raw = ['---', 'name: s', 'description: "a # b"', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(parseSkillContent(raw, 's').description).toBe('a # b')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('description: edited')
    expect(out).not.toContain('# b')
  })

  /* Rounds 6, 7 and 9 each found a different way that weaving a new value into a
     line the author's comment also occupies goes wrong, and the round-9 fix then
     emitted `description: |- # note`, which the BACKEND reader treats as literal
     text and whose content it discards. The splice now declines such a block
     instead of weaving it, so all four arrangements have one answer: edit raw,
     lose nothing. */
  const commentSharedLines: [string, string[]][] = [
    ['an inline comment on a managed field', ['---', 'name: s', 'tags: [a] # context', 'repo_scope: x', '---', '', '# Body']],
    ['a block-scalar header comment', ['---', 'name: s', 'description: | # rationale', '  text', 'repo_scope: x', '---', '', '# Body']],
    ['a folded header comment', ['---', 'name: s', 'description: > # why', '  soft', 'repo_scope: x', '---', '', '# Body']],
    ['a trailing comment on a plain managed value', ['---', 'name: s', 'description: old # note', 'repo_scope: x', '---', '', '# Body']],
  ]

  for (const [label, lines] of commentSharedLines) {
    it(`hands back ${label} for raw editing rather than weaving it`, () => {
      const raw = lines.join('\n')
      const data = parseSkillContent(raw, 's')
      expect(data.frontmatter).toBeUndefined()
      expect(data.raw).toBe(raw)
      expect(assembleSkillContent(data)).toBe(raw)
      expect(canEditStructured(raw)).toBe(false)
    })
  }

  it('keeps a comment ABOVE a field, which is not a shared line', () => {
    /* `commentBefore`, not `comment`: the splice never touches it, so this block
       is still structurally editable and the comment survives a drop. */
    const raw = ['---', 'name: s', '# explains the flag', 'always: true', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), always: false })
    expect(out).toContain('# explains the flag')
    expect(out).not.toContain('always:')
    expect(parseFrontmatter(out).meta.repo_scope).toBe('x')
  })

  it('still splices when only an UNMODELLED field shares a line with a comment', () => {
    const raw = ['---', 'name: s', 'repo_scope: x # scoped', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), description: 'edited' })
    expect(out).toContain('repo_scope: x # scoped')
    expect(parseFrontmatter(out).meta.description).toBe('edited')
  })

  it('never emits a block indicator the backend reader cannot decode', () => {
    /* `|2-` is valid YAML but the backend takes it as the literal value. There is
       no bare form that keeps a leading-space first line, so the indentation is
       lost -- the same bounded loss the previous assembler had -- rather than the
       whole value. */
    const out = assembleSkillContent({
      name: 's', category: '', description: '  indented\nsecond',
      triggers: '', tags: '', always: false, body: '# Body',
    })
    expect(out).not.toMatch(/:\s[|>][+-]?\d/)
    expect(parseFrontmatter(out).meta.description).toBe('indented\nsecond')
  })

  it('does not add backslashes to a value carrying both quote styles', () => {
    const value = 'say "hi" and \'bye\' # note'
    const out = assembleSkillContent({
      name: 's', category: '', description: value,
      triggers: '', tags: '', always: false, body: '# Body',
    })
    expect(out).not.toMatch(/\\"/)
    expect(parseFrontmatter(out).meta.description).toBe(value)
  })

  it('hands back an unclosed frontmatter block for raw editing', () => {
    const raw = '---\nname: s\ntriggers: t\n'
    const data = parseSkillContent(raw, 's')
    expect(canEditStructured(raw)).toBe(false)
    expect(data.raw).toBe(raw)
    expect(assembleSkillContent(data)).toBe(raw)
  })

  it('never emits a form the backend reader cannot decode, for any value', () => {
    /* The property, stated once instead of one test per nasty character. YAML cannot
       carry a C0 control other than tab or newline in scalar content at all -- the
       only YAML form is a double-quoted escape, and the reader does not unescape --
       and asking for a block scalar on such a value is overridden by the library, so
       there is no representation to choose. Those characters are dropped and a
       carriage return is folded to a newline, which is what an HTML control does to
       its own value anyway. */
    const nasty = [
      'plain', 'a\tb', 'ratio: 3', 'a # b', '  padded', 'padded  ', 'one\ntwo',
      'a\\b', 'say "hi" and \'bye\' # note', '  indented\nsecond',
      'a\fb', 'a\vb', 'a\u0001b', 'a\rb', 'a\r\nb', 'ends with "', "'quoted'",
    ]
    for (const value of nasty) {
      const out = assembleSkillContent({
        name: 's', category: '', description: value,
        triggers: '', tags: '', always: false, body: '# Body',
      })
      const line = out.split('\n').find(l => l.startsWith('description:')) ?? ''
      expect(line, JSON.stringify(value)).not.toMatch(/:\s[|>][+-]?\d/)
      expect(line, JSON.stringify(value)).not.toMatch(/:\s(".*\\|'.*\\)/)
    }
  })

  it('does not split a tag that contains a comma', () => {
    /* The form models tags as ONE comma-separated string, so a tag whose own text
       contains a comma has no representation in that input -- editing the field
       would split it into two. */
    const raw = ['---', 'name: s', 'tags: ["docs,api", stable]', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    const data = parseSkillContent(raw, 's')
    expect(data.raw).toBe(raw)
    expect(assembleSkillContent(data)).toBe(raw)
  })

  it('still splices tags whose items carry no comma', () => {
    const raw = ['---', 'name: s', 'tags: [docs, stable]', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(parseSkillContent(raw, 's').tags).toBe('docs, stable')
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), tags: 'docs, beta' })
    expect(out).toContain('tags: [docs, beta]')
  })

  it('declines a managed list whose ITEM carries a comment', () => {
    /* The guard has to look at the whole value subtree, not just its root: editing
       Tags replaces the entire sequence, so a comment on one item goes with it. */
    const raw = ['---', 'name: s', 'tags:', '  - docs # the important one', '  - stable', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('declines a managed list with a comment above an item', () => {
    const raw = ['---', 'name: s', 'triggers:', '  # why these', '  - alpha', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('still splices a managed list with no comments anywhere in it', () => {
    const raw = ['---', 'name: s', 'tags:', '  - docs', '  - stable', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(parseSkillContent(raw, 's').tags).toBe('docs, stable')
  })

  it('declines a managed list holding a non-scalar item', () => {
    /* The form's comma-separated input can only represent a list of plain strings,
       so a map entry has nowhere to go -- reading dropped it and saving wrote the
       list back without it. */
    const raw = ['---', 'name: s', 'tags: [docs, {kind: api}, stable]', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('declines a managed list holding a nested list', () => {
    const raw = ['---', 'name: s', 'triggers: [alpha, [beta, gamma]]', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('declines a managed list holding an empty item', () => {
    const raw = ['---', 'name: s', 'tags: [docs, "", stable]', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('names the inline comment when it is the only obstacle', () => {
    const raw = ['---', 'name: s', 'always: true # why pinned', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(commentIsTheOnlyObstacle(raw)).toBe(true)
  })

  it('stays generic when a second rule also refuses the block', () => {
    /* Naming the comment here would send the user to delete it and leave them in
       the same raw editor, because the comma in the tag refuses the block too. */
    const raw = ['---', 'name: s', 'always: true # why pinned', 'tags: ["docs,api"]', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(commentIsTheOnlyObstacle(raw)).toBe(false)
  })

  it('names no reason for a block the parser could not read', () => {
    const raw = ['---', 'name: a', 'name: b', '---', '', '# Body'].join('\n')
    expect(commentIsTheOnlyObstacle(raw)).toBe(false)
  })

  it('never emits a scalar the reader would strip a boundary quote from', () => {
    /* GPT round 16, measured against the real SKILL_LOADER: it unquotes with
       `value.strip("\"'")`, which cannot tell a wrapping quote from one that belongs
       to the text. Each value must survive a round-trip through the reader, which
       means the writer has to reach for the block literal. */
    for (const value of ['Runs "build"', '"build" runs', '"build"', "it's a mess'", "'quoted'"]) {
      const out = assembleSkillContent({
        name: 's', category: '', description: value,
        triggers: '', tags: '', always: false, body: '# Body',
      })
      expect(parseFrontmatter(out).meta.description, JSON.stringify(value)).toBe(value)
      const line = out.split('\n').find(l => l.startsWith('description:')) ?? ''
      expect(line.replace(/^description:\s*/, ''), JSON.stringify(value)).toMatch(/^\|/)
    }
  })

  it('still writes a plain scalar when no quote sits at a boundary', () => {
    /* The guard is anchored, not global: an interior quote survives the reader, so
       forcing a block literal for it would be needless churn. */
    const out = assembleSkillContent({
      name: 's', category: '', description: 'say "hi" now',
      triggers: '', tags: '', always: false, body: '# Body',
    })
    expect(out).toContain('description: say "hi" now')
  })

  it('declines frontmatter carrying a document-end marker', () => {
    /* `...` ends the YAML document. Appending a missing managed field after it puts
       the new key outside the document the reader will parse. */
    const raw = ['---', 'name: s', 'repo_scope: x', '...', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('does not strand an added field after a document-end marker', () => {
    /* The consequence GPT named: adding a field that was absent appends it, and an
       append lands after `...`, where the reader will never see it. Refusal routes
       the block to the raw editor, so no splice happens and nothing is stranded. */
    const raw = ['---', 'name: s', 'repo_scope: x', '...', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 's')
    expect(data.raw).toBeDefined()
    expect(assembleSkillContent({ ...data, tags: 'docs' })).toBe(raw)
  })

  it('does not mistake other dot runs for a document-end marker', () => {
    /* Only a bare `...` at column 0 is the marker: a value that happens to be `...`,
       or one inside a block scalar, is just text and must stay editable. (A bare
       `....` line is refused too, but by the parse-error rule -- it is not valid
       inside a mapping -- so it is not evidence about this rule.) */
    for (const line of ['description: ...', 'description: |-\n  ...']) {
      const raw = ['---', 'name: s', line, 'repo_scope: x', '---', '', '# Body'].join('\n')
      expect(parseSkillContent(raw, 's').raw, JSON.stringify(line)).toBeUndefined()
    }
  })

  it('declines a managed list holding a multiline item', () => {
    /* A single-line comma-separated input cannot hold a newline, so the item's
       lines would be merged on save. Same requirement as the non-scalar case --
       "representable in that field" -- which I had stated but only half enforced. */
    const raw = ['---', 'name: s', 'tags:', '  - |-', '    docs', '    api', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('keeps a folded list item editable, because folding leaves no newline', () => {
    /* `>-` folds single breaks to spaces, so this item's VALUE is `alpha beta` with
       no newline in it -- the single-line input can carry that faithfully. Checked
       rather than assumed: I first expected this to be refused alongside the literal
       case, and the value proves otherwise. */
    const raw = ['---', 'name: s', 'triggers:', '  - >-', '    alpha', '    beta', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(parseSkillContent(raw, 's').triggers).toBe('alpha beta')
    expect(canEditStructured(raw)).toBe(true)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
    // Editing the list keeps the item's text; only its rendering changes.
    const out = assembleSkillContent({ ...parseSkillContent(raw, 's'), triggers: 'alpha beta, gamma' })
    expect(parseFrontmatter(out).meta.triggers).toContain('alpha beta')
  })

  it('declines a managed list holding a padded item', () => {
    /* Not in the finding, but the same requirement: the save path trims each piece,
       so an item whose text carries deliberate padding cannot come back unchanged.
       Found by stating the round-trip rather than listing shapes. */
    const raw = ['---', 'name: s', 'tags: ["  padded", stable]', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('never emits a single-quoted scalar carrying a doubled apostrophe', () => {
    /* GPT round 19 claimed `it''s` could reach the file. It cannot, and the reason is
       structural rather than lucky -- `yaml`'s own quote selection
       (stringifyString.js `quotedString`) picks single quotes ONLY in the
       `hasDouble && !hasSingle` branch, where the `replace(/'/g, "''")` inside
       `singleQuotedString` has nothing to match. With both quote styles present it
       picks double quotes, because this writer never sets the `singleQuote` option,
       and it never sets a node type of QUOTE_SINGLE either (only BLOCK_LITERAL).
       Pinned as a property here rather than argued in prose, so a future option
       change or library upgrade fails this test instead of corrupting a value. */
    const alphabet = ['a', ' ', '"', "'", ':', ': ', '#', '\\', 'z', '.']
    let seed = 7
    const next = (n: number) => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) % n
    for (let i = 0; i < 300; i++) {
      let value = ''
      for (let j = 0; j < 1 + next(6); j++) value += alphabet[next(alphabet.length)]
      if (!value.trim()) continue
      const out = assembleSkillContent({
        name: 's', category: '', description: value,
        triggers: '', tags: '', always: false, body: '# Body',
      })
      const rhs = (out.split('\n').find(l => l.startsWith('description:')) ?? '')
        .replace(/^description:\s*/, '')
      if (rhs.startsWith("'")) expect(rhs, JSON.stringify(value)).not.toContain("''")
      expect(parseFrontmatter(out).meta.description, JSON.stringify(value)).toBe(value)
    }
  })

  it('declines a managed list written as a multiline scalar', () => {
    /* The round-18 guard checked SEQUENCE items and waved the scalar form through.
       A block-literal `triggers` has a newline the single-line input cannot hold. */
    const raw = ['---', 'name: s', 'triggers: |-', '  alpha', '  beta', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('declines a multiline scalar tags value', () => {
    const raw = ['---', 'name: s', 'tags: |-', '  docs', '  api', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('keeps the comma-separated scalar form the editor itself writes', () => {
    /* Commas in the SCALAR form are the field's own separator, so they round-trip by
       design -- the asymmetry with sequence items is deliberate, not an oversight. */
    const raw = ['---', 'name: s', 'triggers: alpha, beta', 'tags: docs, api', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(parseSkillContent(raw, 's').triggers).toBe('alpha, beta')
  })

  it('declines a managed scalar the backend reads differently than YAML does', () => {
    /* `"first\\nsecond"` is a real newline to a YAML parser and the two literal
       characters backslash-n to `SKILL_LOADER`, which does not unescape. Adopting the
       parser's reading and saving would silently redefine the backend-visible value,
       so the block has to be edited raw. This is a READ-side mirror of the writer's
       own rule, and a divergence main did not have: main read with the same dialect
       it wrote with. */
    const raw = ['---', 'name: s', 'description: "first\\nsecond"', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('declines an existing plain scalar whose boundary quote the backend eats', () => {
    /* Read side. The file already says `Runs "build"`; the backend reads
       `Runs "build`. The two dialects disagree about what this file currently means,
       so the form must not adopt one reading and save it. Distinct from the WRITE-side
       rule above: a value with boundary quotes TYPED into the form is emitted as a
       block literal, because there the user's intent is unambiguous. */
    const raw = ['---', 'name: s', 'description: Runs "build"', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('keeps a scalar with interior quotes editable', () => {
    /* Both dialects agree here -- the backend only strips quotes at the boundaries --
       so refusing this would be over-reach. */
    const raw = ['---', 'name: s', 'description: say "hi" now', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(parseSkillContent(raw, 's').description).toBe('say "hi" now')
  })

  it('declines a managed list whose value is a mapping', () => {
    /* Sixth version of this guard. Round 20 fixed the SCALAR branch and left the
       catch-all `return true`, so a mapping-valued `tags` was still waved through and
       an edit would replace the whole mapping with a string sequence. */
    const raw = ['---', 'name: s', 'tags: {kind: api}', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('declines a block-mapping triggers value', () => {
    const raw = ['---', 'name: s', 'triggers:', '  kind: api', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('still allows a managed list key with no value at all', () => {
    /* `tags:` with nothing after it must stay spliceable -- an earlier round fixed it
       being deleted outright, and default-deny must not undo that. */
    const raw = ['---', 'name: s', 'tags:', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('writes flow tags in the spacing this editor has always used', () => {
    /* First Principles review: the library pads flow collections to `[ a, b ]`, which
       would rewrite this line in every edited file for no reason. Main wrote
       `tags: [${data.tags}]`. Item separators are normalised to `, ` -- main echoed the
       typed string verbatim -- but that only ever affects a field the user just edited. */
    for (const typed of ['a, b', 'a,b', 'a ,  b']) {
      const out = assembleSkillContent({
        name: 's', category: '', description: '', triggers: '',
        tags: typed, always: false, body: '# Body',
      })
      expect(out, JSON.stringify(typed)).toContain('tags: [a, b]')
    }
  })

  it('never returns a rendering its own guard rejects', () => {
    /* GPT round 23: the last line of `renderManaged` returned a quoted scalar without
       re-checking the contract the whole function exists to enforce. A value that is a
       newline followed by indented text defeats both block attempts, so it fell through
       and shipped `description: |2-` -- an explicit indentation indicator, which makes
       the backend take the header as the value and discard the text entirely. Stated as
       the property rather than the one value: no managed rendering may be a quoted
       scalar carrying a backslash escape, nor an explicit indentation indicator. */
    const values = ['\n  indented', '\n\n  x', '  \n  y', '\nplain', '\n\ttabbed']
    for (const value of values) {
      const out = assembleSkillContent({
        name: 's', category: '', description: value,
        triggers: '', tags: '', always: false, body: '# Body',
      })
      const line = out.split('\n').find(l => l.startsWith('description:')) ?? ''
      const rhs = line.replace(/^description:\s*/, '')
      expect(rhs, JSON.stringify(value)).not.toMatch(/^["'].*\\/)
      expect(rhs, JSON.stringify(value)).not.toMatch(/^[|>][+-]?\d/)
      // The value survives to the reader in some readable form, never an empty one.
      expect(parseFrontmatter(out).meta.description, JSON.stringify(value)).toBeTruthy()
      /* And the loss stays MINIMAL: these keep their text as a block literal. Without
         the leading-blank-line strip they still decode, but only by falling through to
         the flattening last resort -- a mutation test showed the contract assertions
         above cannot tell those two apart, so this pins the better outcome. */
      expect(rhs, JSON.stringify(value)).toMatch(/^\|/)
    }
  })

  it('declines an existing block scalar with an explicit indentation indicator', () => {
    /* GPT round 24, read side. The ORIGINAL reason was that `SKILL_LOADER` resolved only
       the six BARE indicators, so given `|2-` it took the header itself as the value while
       the YAML parser decoded the body -- the two disagreed about what the file already
       meant. #7097 fixed that: the backend now resolves the full header grammar and agrees
       with the parser here.
       The refusal REMAINS, for a narrower reason: this file does not simulate the explicit
       indent, so it has no value to compare and must not guess. That only declines an edit
       the backend could have taken -- conservative, never corrupting -- and relaxing it is
       a capability change belonging with the folded-family work #7187 deferred. */
    const raw = ['---', 'name: s', 'description: |2-', '  body text', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(false)
    expect(assembleSkillContent(parseSkillContent(raw, 's'))).toBe(raw)
  })

  it('simulates the literal fold the way the reader does', () => {
    /* `backendFoldsLiteral` reproduces `fold_block_scalar` for the `|` family, and a wrong
       simulation means a wrong comparison. Each expected string below was measured from
       the real Python reader, so this test fails if either side drifts. Reached through
       the public behaviour: a field whose simulated backend value MATCHES the parser's is
       editable, one that differs is refused. */
    const editable: [string, string, string[]][] = [
      ['plain two lines', '|', ['  one', '  two']],
      ['interior blank', '|', ['  one', '', '  two']],
      ['deeper indent inside', '|', ['  one', '    nested', '  two']],
      /* These two used to be refused: the reader collapsed them to something the parser
         did not, because its fold ended in `.strip()`. #7097 made it honour chomping and
         keep a leading break, so both sides now agree and the capability comes back. */
      ['leading blank', '|', ['', '  true']],
      ['keep chomp trailing blanks', '|+', ['  true', '', '']],
      ['clip chomp trailing blank', '|', ['  one', '']],
      /* Whitespace shapes, each of which used to make the simulation disagree with the
         reader and so refuse an edit the backend could take. A trailing line of three
         spaces is a one-space CONTENT line once dedented, not a break; a content line
         keeps the spaces its author typed at the end; and indentation is counted in
         SPACES, so two spaces then a tab is a tab of content -- which also means a bare
         tab line ends the block, exactly as the reader's own walk decides. */
      ['whitespace-only trailing line', '|', ['  one', '   ']],
      ['content with trailing spaces', '|', ['  one  ', '  two  ']],
      ['tab past the indent', '|', ['  \t']],
      ['tab then deeper then flush', '|', ['  \t', '    deep', '  one']],
      ['trailing whitespace and a blank', '|-', ['  one  ', '   ', '']],
    ]
    for (const [label, ind, body] of editable) {
      const raw = ['---', 'name: s', `description: ${ind}`, ...body, 'repo_scope: x', '---', '', '# Body'].join('\n')
      expect(canEditStructured(raw), label).toBe(true)
    }
    /* Still refused, and for a reason the simulation cannot remove: the FOLDED family's
       rules are not reproduced here at all, so there is no simulated value to compare. */
    const refused: [string, string, string[]][] = [
      ['folded', '>', ['  one', '  two']],
      ['folded keep chomp', '>+', ['  one', '', '']],
    ]
    for (const [label, ind, body] of refused) {
      const raw = ['---', 'name: s', `description: ${ind}`, ...body, 'repo_scope: x', '---', '', '# Body'].join('\n')
      expect(canEditStructured(raw), label).toBe(false)
    }
  })

  it('compares block scalars instead of trusting their indicator', () => {
    /* GPT rounds 24-26 walked this boundary three times: six indicators exempt, then
       four, then the realisation that agreement depended on the CONTENT -- the reader's
       fold ended in `.strip()`, which ate LEADING whitespace, something no YAML chomping
       mode does. #7097 removed that step, so the `|` family now agrees outright; the
       SIMULATION stays because it is what catches the next drift, and because the dedent
       rule still is not derivable from the indicator. A bare LITERAL scalar is SIMULATED
       and compared, which keeps it editable when the two readings match; a FOLDED form is
       refused because reproducing those rules to compare is the coupling #7187 avoided,
       and an explicit indicator is refused for the same reason even though the backend
       now resolves it -- refusing merely declines an edit, it cannot corrupt. */
    const literal = ['---', 'name: s', 'description: |', '  body text', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(literal), 'plain literal agrees').toBe(true)
    for (const ind of ['>', '>-', '>+', '|2-']) {
      const raw = ['---', 'name: s', `description: ${ind}`, '  body text', 'repo_scope: x', '---', '', '# Body'].join('\n')
      expect(canEditStructured(raw), ind).toBe(false)
      expect(assembleSkillContent(parseSkillContent(raw, 's')), ind).toBe(raw)
    }
  })

  it('edits a block scalar whose blank first line the reader used to eat', () => {
    /* The case that ended the indicator approach: `|-` is a STRIP form, so nothing about
       the indicator hinted at a problem -- the divergence came from where the content
       sat, because the reader's fold ended in `.strip()`. #7097 removed that step, so a
       leading break now survives on BOTH sides and the field is editable. The comparison
       is what proves it, not the indicator. */
    const raw = ['---', 'name: s', 'always: |-', '', '  true', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    /* And the flag still reads as ON: the value is `\ntrue`, which only equals `true`
       once trimmed -- the same normalisation the loader applies. Getting this wrong shows
       the skill as off in the form while the loader keeps injecting it. */
    expect(parseSkillContent(raw, 's').always).toBe(true)
  })

  it('keeps a keep-chomped always disableable', () => {
    /* Was 'does not let a keep-chomped always become undisableable', which refused the
       block because the reader dropped the trailing breaks and the parser kept them. Both
       now read `true\n\n`, so the field is editable -- and the round trip below is the
       part that matters: turning the checkbox OFF must actually reach the file, not be
       swallowed by `managedUnchanged` comparing an untrimmed `true\n\n` against `true`. */
    const raw = ['---', 'name: s', 'always: |+', '  true', '', '', 'repo_scope: x', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    const data = parseSkillContent(raw, 's')
    expect(data.always).toBe(true)
    const off = assembleSkillContent({ ...data, always: false })
    expect(off).not.toBe(raw)
    expect(parseSkillContent(off, 's').always).toBe(false)
  })

  it('reads an uppercase always the way the loader does', () => {
    /* GPT 5.6 review, head 9cc2cfa32. The loader reads this flag as
       `.strip().lower() == "true"`, so `always: "TRUE"` activates the skill. The form
       compared case-sensitively, so it showed the same skill as OFF -- the checkbox
       disagreeing with the loader about whether the skill injects. Pre-existing in the
       case dimension; fixed here because this PR is what makes the two comparisons
       claim to agree. */
    for (const spelling of ['"TRUE"', 'True', '  true  ']) {
      const raw = ['---', 'name: s', `always: ${spelling}`, 'repo_scope: x', '---', '', '# Body'].join('\n')
      expect(parseSkillContent(raw, 's').always, spelling).toBe(true)
    }
    /* ...and a genuinely false-like value still reads as off, so the normalisation did
       not turn the comparison into "any non-empty value activates". */
    for (const spelling of ['false', 'FALSE', 'no', '0', '']) {
      const raw = ['---', 'name: s', `always: ${spelling}`, 'repo_scope: x', '---', '', '# Body'].join('\n')
      expect(parseSkillContent(raw, 's').always, spelling).toBe(false)
    }
  })

  it('leaves a skill with no frontmatter alone', () => {
    const data = parseSkillContent('# Just a body\n', 'plain')
    expect(data.frontmatter).toBeUndefined()
    expect(assembleSkillContent(data)).toContain('# Just a body')
  })

  it('writes a fresh block for a brand-new skill', () => {
    const out = assembleSkillContent({
      name: 'fresh', category: '', description: 'what it does',
      triggers: 'a, b', tags: 'x, y', always: true, body: '# Body',
    })
    expect(out).toBe('---\nname: fresh\ndescription: what it does\nalways: true\ntriggers: a, b\ntags: [x, y]\n---\n\n# Body')
  })

  it('quotes a value that would otherwise change the YAML structure', () => {
    /* The old assembler interpolated the field into `key: value`, so a colon or
       a leading `#` in a description produced a block the parser reads back
       differently. The writer now escapes what it must. */
    const out = assembleSkillContent({
      name: 's', category: '', description: 'ratio: 3 # not a comment',
      triggers: '', tags: '', always: false, body: '# Body',
    })
    expect(parseFrontmatter(out).meta.description).toBe('ratio: 3 # not a comment')
  })

  it('does not reflow a long single-line description', () => {
    const long = `${'word '.repeat(40).trim()}`
    const out = assembleSkillContent({
      name: 's', category: '', description: long,
      triggers: '', tags: '', always: false, body: '# Body',
    })
    expect(out).toContain(`description: ${long}\n`)
    expect(parseFrontmatter(out).meta.description).toBe(long)
  })

  it('round-trips a multi-line description without gaining a newline per save', () => {
    const first = assembleSkillContent({
      name: 's', category: '', description: 'line one\nline two',
      triggers: '', tags: '', always: false, body: '# Body',
    })
    const second = assembleSkillContent(parseSkillContent(first, 's'))
    expect(parseFrontmatter(second).meta.description).toBe('line one\nline two')
    expect(second).toBe(first)
  })

  it('reads triggers written as a YAML sequence as a comma list', () => {
    const raw = ['---', 'name: s', 'triggers:', '  - alpha', '  - beta', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 's')
    expect(data.triggers).toBe('alpha, beta')
    // Untouched, so the sequence form is kept rather than flattened.
    expect(assembleSkillContent(data)).toContain('triggers:\n  - alpha\n  - beta')
  })

  it('does not show the newline a clipped block scalar implies', () => {
    /* `description: |` clips to a value ending in "\n". The textarea and the meta
       strip must not show that phantom blank line, and it must not be carried
       back into an edited value. */
    const raw = ['---', 'name: s', 'description: |', '  one', '  two', '---', '', '# Body'].join('\n')
    expect(parseSkillContent(raw, 's').description).toBe('one\ntwo')
    expect(parseFrontmatter(raw).meta.description).toBe('one\ntwo')

    const raw2 = ['---', 'name: s', 'description: >', '  soft', '  wrapped', '---', '', '# Body'].join('\n')
    expect(parseSkillContent(raw2, 's').description).toBe('soft wrapped')
  })
})

describe('a frontmatter block the parser rejects is edited raw', () => {
  /* Third element: whether the PARSER produces a message. The two structural
     cases are valid YAML the form simply cannot represent, so there is nothing
     for the parser to complain about — the structured affordance is withheld
     instead. */
  const cases: [string, string, boolean][] = [
    ['a duplicate key', ['---', 'name: a', 'name: b', '---', '', '# Body'].join('\n'), true],
    ['a tab used as indentation', ['---', 'name: s', 'nested:', '\t- a', '---', '', '# Body'].join('\n'), true],
    ['an unclosed quote', ['---', 'name: "unclosed', 'description: d', '---', '', '# Body'].join('\n'), true],
    ['a sequence at the root', ['---', '- just', '- a list', '---', '', '# Body'].join('\n'), false],
    ['a flow mapping at the root', ['---', '{ name: s }', '---', '', '# Body'].join('\n'), false],
    /* An anchor on a MANAGED field is aliased by an unmodelled one, so
       re-rendering the field would drop the anchor and leave `note: *shared`
       dangling -- a saved file that no longer parses. */
    ['an anchor a later key aliases', ['---', 'name: s', 'description: &shared old', 'note: *shared', '---', '', '# Body'].join('\n'), false],
    ['an anchor on an unmodelled key', ['---', 'name: s', 'anchored: &ref keep me', '---', '', '# Body'].join('\n'), false],
    /* GPT 5.6 review, head d5cc48c98. Both are valid YAML the splice cannot
       rewrite faithfully: an explicit key puts `? ` before the key, so replacing
       the key's own range would leave the marker behind, and a root-indented
       mapping would get an appended field at a different indentation from its
       siblings -- a YAML error, not a cosmetic difference. */
    ['an explicit key', ['---', 'name: s', '? complex', ': value', '---', '', '# Body'].join('\n'), false],
    ['a root-indented mapping', ['---', '  name: s', '  repo_scope: x', '---', '', '# Body'].join('\n'), false],
  ]

  for (const [label, raw, hasParserMessage] of cases) {
    it(`hands back ${label} untouched instead of rewriting it`, () => {
      const data = parseSkillContent(raw, 's')
      expect(data.frontmatter).toBeUndefined()
      expect(data.raw).toBe(raw)
      // Nothing is lost: a save writes the original bytes back.
      expect(assembleSkillContent(data)).toBe(raw)
      // And the structured editor is not offered for it.
      expect(canEditStructured(raw)).toBe(false)
      expect(frontmatterError(raw) !== null).toBe(hasParserMessage)
    })
  }

  it('offers the structured editor for a block the parser accepts', () => {
    const raw = ['---', 'name: s', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(frontmatterError(raw)).toBeNull()
  })

  it('offers the structured editor when there is no frontmatter at all', () => {
    expect(canEditStructured('# Just a body\n')).toBe(true)
    expect(frontmatterError('# Just a body\n')).toBeNull()
  })

  it('offers the structured editor for an empty frontmatter block', () => {
    const raw = ['---', '---', '', '# Body'].join('\n')
    expect(canEditStructured(raw)).toBe(true)
    expect(assembleSkillContent({ ...parseSkillContent(raw, 's'), name: 's' })).toContain('name: s')
  })

  it('reports a line number that matches what the textarea shows', () => {
    /* UX review, head 967889b0e: `block` starts after the opening `---`, so the
       parser's own line numbers sat one above the line a user counts in the
       textarea. The duplicate key below is on textarea line 4. The trailing colon
       introduced the caret excerpt, which is dropped, so it goes too. */
    const raw = ['---', 'name: a', 'triggers: t', 'name: b', '---', '', '# Body'].join('\n')
    const msg = frontmatterError(raw)
    expect(msg).toContain('line 4')
    expect(msg).not.toContain('\n')
    expect(msg?.endsWith(':')).toBe(false)
  })

  it('still shows what it could read from a malformed block', () => {
    /* Display is tolerant where writing is strict: a meta strip over a file with
       a duplicate key should still show the fields, since rendering cannot
       corrupt anything. */
    const raw = ['---', 'name: a', 'name: b', 'triggers: t', '---', '', '# Body'].join('\n')
    expect(parseFrontmatter(raw).meta.triggers).toBe('t')
    expect(parseFrontmatter(raw).body).toBe('# Body')
  })
})
