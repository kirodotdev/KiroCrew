import { describe, expect, it } from 'vitest'

import { assembleSkillContent, parseFrontmatter, parseSkillContent } from '../components/SkillForm'

/**
 * The structured skill editor rebuilds the frontmatter block from its own
 * fields, so any key it does not model is destroyed on save. Two runtime keys
 * live in that blind spot: `repo_scope` (the matcher's repo guard) and
 * `inject_on_trigger` (the full-body opt-out). These pin that a round-trip
 * carries them through.
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

  it('does not duplicate a managed key that also appears in extra', () => {
    const data = parseSkillContent(RAW, 'kirocrew-dev/worktree-dev')
    const out = assembleSkillContent({ ...data, extra: { ...data.extra, name: 'name: smuggled' } })
    expect(out.match(/^name:/gm)).toHaveLength(1)
    expect(parseFrontmatter(out).meta.name).toBe('worktree-dev')
  })

  /**
   * The form has no idea what YAML type an unmodelled field is, so it must
   * re-emit the ORIGINAL lines rather than reserialize a parsed value. Emitting
   * `key: |` for anything multi-line would turn a list or a nested map into a
   * literal string — the field would survive the round-trip in shape but not in
   * meaning, which is worse than losing it loudly.
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
    expect(out).toContain('note: >')
    expect(out).not.toContain('note: |')
  })

  it('keeps a blank line inside a multiline value', () => {
    /* A paragraph break inside a block scalar is content. Dropping it rewrites
       the author's text while they were editing something else. */
    const raw = ['---', 'name: s', 'note: |', '  one', '', '  two', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('note: |\n  one\n\n  two')
  })

  it('does not invent a trailing blank line inside the frontmatter', () => {
    const raw = ['---', 'name: s', 'note: |', '  one', '', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    expect(out).toContain('note: |\n  one\n---')
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
    ]
    const raw = ['---', 'name: s', 'description: d', ...blocks, '---', '', '# Body'].join('\n')

    const out = assembleSkillContent(parseSkillContent(raw, 's'))

    for (const block of blocks) expect(out).toContain(block)
  })

  /* The scalar view and the verbatim view need different rules. A top-level
     comment is a continuation for PRESERVATION purposes but not for VALUE
     purposes — folding it into the value made `always` read as unset, so the
     form dropped the pin. */
  it('does not let a top-level comment corrupt a modelled flag', () => {
    const raw = ['---', 'name: s', 'always: true', '# why it is pinned', 'triggers: t', '---', '', '# Body'].join('\n')
    const data = parseSkillContent(raw, 's')
    expect(data.always).toBe(true)
    const meta = parseFrontmatter(assembleSkillContent(data)).meta
    expect(meta.always).toBe('true')
    expect(meta.triggers).toBe('t')
  })

  it('does not let a top-level comment corrupt an unmodelled scalar', () => {
    const raw = ['---', 'name: s', 'repo_scope: src/kiro_crew', '# scoped on purpose', '---', '', '# Body'].join('\n')
    const out = assembleSkillContent(parseSkillContent(raw, 's'))
    const meta = parseFrontmatter(out).meta
    expect(meta.repo_scope).toBe('src/kiro_crew')
    // Preserved verbatim, comment included, because nothing models this key.
    expect(out).toContain('repo_scope: src/kiro_crew\n# scoped on purpose')
  })

  it('leaves a skill with no frontmatter alone', () => {
    const data = parseSkillContent('# Just a body\n', 'plain')
    expect(data.extra ?? {}).toEqual({})
    expect(assembleSkillContent(data)).toContain('# Just a body')
  })
})
