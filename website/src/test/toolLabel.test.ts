import { describe, it, expect } from 'vitest'
import { commandFromToolInput, deriveShellSummary, DERIVE_LABEL_THRESHOLD_CHARS, pickToolLabel } from '../utils/toolLabel'

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
    expect(deriveShellSummary(label)).toBe('Running: docker-compose up, tee')
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
    expect(deriveShellSummary('Running: make build 2>&1')).toBe('Running: make build')
  })

  it('skips env assignments before the binary', () => {
    expect(deriveShellSummary('Running: FOO=1 BAR=2 python3 -m pytest')).toBe('Running: python3')
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


  it('names the script behind an interpreter, with its subcommand', () => {
    expect(deriveShellSummary('Running: python3 ledger.py ticket-log --id P1 --type root-cause')).toBe(
      'Running: ledger.py ticket-log',
    )
    // Versioned interpreter, and inline code keeps the interpreter name.
    expect(deriveShellSummary('Running: python3.12 tool.py')).toBe('Running: tool.py')
    expect(deriveShellSummary("Running: python3 -c 'print(1)'")).toBe('Running: python3')
  })

  it('steps over variable-reference and wrapper heads', () => {
    // The observed oncall shape: interpreter behind a shell variable, after a
    // cd — the readable name is the script and its subcommand.
    const cmd = 'Running: PY=/app/bin/python3; cd ~/backend && $PY ledger.py ticket-log --id X'
    expect(deriveShellSummary(cmd)).toBe('Running: ledger.py ticket-log')
    expect(deriveShellSummary('Running: sudo make install')).toBe('Running: make install')
  })

  it('attaches the subcommand only to the first meaningful name', () => {
    expect(deriveShellSummary('Running: git fetch && git rebase upstream/main')).toBe('Running: git fetch')
    expect(deriveShellSummary('Running: cd /repo && npm run build | tee log')).toBe('Running: npm run, tee')
  })

  it('pickToolLabel still falls back to the raw label the summary substitutes for', () => {
    const raw = "cat > /tmp/desc.md <<'EOF'\nlong body\nEOF"
    const picked = pickToolLabel({ simplified: true, purpose: '', rawLabel: raw, uiLang: 'en' })
    expect(picked).toBe(raw)
    expect(raw.length > DERIVE_LABEL_THRESHOLD_CHARS || raw.includes('\n')).toBe(true)
  })
})

describe('commandFromToolInput', () => {
  it('reads the command out of raw-input JSON', () => {
    expect(commandFromToolInput('{"command": "git status", "__tool_use_purpose": "check"}')).toBe('git status')
  })
  it('returns null for non-JSON, non-object, or command-less input', () => {
    expect(commandFromToolInput('')).toBeNull()
    expect(commandFromToolInput('not json')).toBeNull()
    expect(commandFromToolInput('[1,2]')).toBeNull()
    expect(commandFromToolInput('{"operations": []}')).toBeNull()
    expect(commandFromToolInput('{"command": "  "}')).toBeNull()
  })
})
