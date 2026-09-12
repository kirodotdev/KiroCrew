import { describe, it, expect } from 'vitest'
import { deriveShellSummary, pickToolLabel } from '../utils/toolLabel'

describe('deriveShellSummary', () => {
  it('summarizes a heredoc write to binary plus redirect target', () => {
    const label = "Running: cat > /tmp/cr3_desc.md <<'EOF'\n### Notes\nbody\nEOF"
    expect(deriveShellSummary(label)).toBe('Running: cat → /tmp/cr3_desc.md')
  })

  it('lists the binaries of a chained pipeline, demoting bookkeeping and capping', () => {
    const label = 'Running: export PATH="/usr/local/bin:$PATH" cd /repo; ls -a | grep -i crux || echo none; git -P log --oneline -1; wc -l x'
    // `export` is dropped in favour of binaries that say what the command DOES.
    expect(deriveShellSummary(label)).toBe('Running: ls, grep, echo, git …')
  })

  it('keeps a bookkeeping builtin when it is the whole command', () => {
    expect(deriveShellSummary('Running: cd /some/very/long/path')).toBe('Running: cd')
  })

  it('reads past a bookkeeping first line in a multi-line script', () => {
    const label = 'Running: export PATH=/usr/local/bin\ndocker-compose up --build | tee /tmp/build.log'
    expect(deriveShellSummary(label)).toBe('Running: docker-compose, tee')
  })

  it('stops parsing at a heredoc so body lines contribute no binaries', () => {
    const label = "Running: cat > /tmp/x.md <<'EOF'\ngit status\nls -a\nEOF"
    // git/ls inside the heredoc are document text, not commands.
    expect(deriveShellSummary(label)).toBe('Running: cat → /tmp/x.md')
  })

  it('does not split on operators inside quotes', () => {
    expect(deriveShellSummary("Running: grep -E 'foo|bar' file.txt")).toBe('Running: grep')
  })

  it('does not treat 2>&1 as a segment or a target', () => {
    expect(deriveShellSummary('Running: make build 2>&1')).toBe('Running: make')
  })

  it('skips env assignments before the binary', () => {
    expect(deriveShellSummary('Running: FOO=1 BAR=2 python3 -m pytest')).toBe('Running: python3')
  })

  it('names the binaries inside a loop, not the loop keywords', () => {
    expect(deriveShellSummary('for f in src/*.ts; do wc -l "$f"; done', { bareCommand: true })).toBe('wc')
    expect(deriveShellSummary('if [ -f x ]; then cat x; else echo none; fi', { bareCommand: true })).toBe('cat, echo')
  })

  it('returns null for MCP invocations and non-shell titles', () => {
    expect(deriveShellSummary('Running: @kirocrew-core/spawn_run')).toBeNull()
    expect(deriveShellSummary('Editing AGENTS.md')).toBeNull()
  })

  it('derives a bare-command label only when the caller vouches it is shell', () => {
    // Real flooding sessions persist bare titles without the Running: prefix —
    // one observed session had 126 of 126 shell calls in that shape. is_shell
    // from the tool log is the gate; the shape alone is not trusted.
    const bare = 'export PATH="/usr/local/bin:$PATH" cd /repo; ls -a | grep -i crux'
    expect(deriveShellSummary(bare)).toBeNull()
    expect(deriveShellSummary(bare, { bareCommand: true })).toBe('ls, grep')
    // The vouch does not let MCP-shaped labels through.
    expect(deriveShellSummary('@kirocrew-core/spawn_run', { bareCommand: true })).toBeNull()
  })

  it('pickToolLabel still falls back to the raw label the summary substitutes for', () => {
    const raw = "cat > /tmp/desc.md <<'EOF'\nlong body\nEOF"
    const picked = pickToolLabel({ simplified: true, purpose: '', rawLabel: raw, uiLang: 'en' })
    expect(picked).toBe(raw)
  })
})

describe('pickToolLabel base label (derivedTitle)', () => {
  const uiLang = 'en'
  it('shows the derived title in simplified mode', () => {
    expect(pickToolLabel({ simplified: true, rawLabel: 'Running: git status', derivedTitle: 'Git status', uiLang })).toBe('Git status')
  })
  it('keeps an informative raw title in raw mode', () => {
    expect(pickToolLabel({ simplified: false, rawLabel: 'Running: git status', derivedTitle: 'Git status', uiLang })).toBe('Running: git status')
  })
  it('replaces a stub raw title with the derived title even in raw mode', () => {
    expect(pickToolLabel({ simplified: false, rawLabel: 'shell', derivedTitle: 'List files in src', uiLang })).toBe('List files in src')
    expect(pickToolLabel({ simplified: false, rawLabel: '', derivedTitle: 'List files in src', uiLang })).toBe('List files in src')
  })
  it('lets a same-language purpose replace the derived title only in simplified mode', () => {
    expect(pickToolLabel({ simplified: true, purpose: 'Check the tree', rawLabel: 'git status', derivedTitle: 'Git status', uiLang })).toBe('Check the tree')
    expect(pickToolLabel({ simplified: false, purpose: 'Check the tree', rawLabel: 'git status', derivedTitle: 'Git status', uiLang })).toBe('git status')
  })
  it('falls back to the derived title when the purpose is in another script', () => {
    expect(pickToolLabel({ simplified: true, purpose: '查看状态', rawLabel: 'git status', derivedTitle: 'Git status', uiLang })).toBe('Git status')
  })
})
