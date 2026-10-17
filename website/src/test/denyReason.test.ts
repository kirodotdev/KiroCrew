import { describe, it, expect } from 'vitest'
import { extractDenyDetail, extractDenyNotice, extractDenyReason } from '../utils/denyReason'

// The Output panel of a blocked tool call used to show a fixed
// "blocked by security policy" line and discard the row's real content, so the
// user could never see WHICH rule fired. These pin the extraction that replaces
// that placeholder, and the cases where the placeholder must still win.
describe('extractDenyReason', () => {
  const ROW =
    '🚫 Running: python3 -c "import x" — Blocked by security policy: ' +
    'kiro[-.]?crew\\b[^|;&#>/*]*\\btoken\\b\n' +
    "Matched structurally on the command's argv, not by the pattern text above."

  it('returns the reason starting at the contract marker', () => {
    expect(extractDenyReason(ROW)).toMatch(/^Blocked by security policy:/)
  })

  it('drops the row title so the panel shows the reason, not the command', () => {
    const out = extractDenyReason(ROW)
    expect(out).not.toContain('🚫')
    expect(out).not.toContain('Running:')
  })

  it('keeps the pattern that fired', () => {
    expect(extractDenyReason(ROW)).toContain('\\btoken\\b')
  })

  it('keeps the second explanation line', () => {
    // The structural note is the part that makes a floor hit intelligible; a
    // single-line extraction would silently drop exactly that.
    expect(extractDenyReason(ROW)).toContain('Matched structurally')
  })

  it('yields empty for a row with no reason, so the placeholder wins', () => {
    expect(extractDenyReason('🚫 shell (hook blocked)')).toBe('')
    expect(extractDenyReason('🚫 shell')).toBe('')
  })

  it('reads the host reason, not a model-authored title that mimics it', () => {
    // `<title>` prefers the tool call's own `description` field, so it is
    // model-authored. First-match extraction would render the model's own text
    // to the user AS the security reason; the host always appends the real one
    // after the title, so the LAST marker is the trustworthy one.
    const spoofed =
      '🚫 Running: Blocked by security policy: totally fine, ignore this' +
      ' — Blocked by security policy: real-deny-rule'
    const out = extractDenyReason(spoofed)
    expect(out).toBe('Blocked by security policy: real-deny-rule')
    expect(out).not.toContain('totally fine')
  })

  it('still reads a spoof-shaped title when the host reason carries a note', () => {
    const spoofed =
      '🚫 Running: Blocked by security policy: spoof — Blocked by security policy: rule\n' +
      'Matched structurally on the argv.'
    const out = extractDenyReason(spoofed)
    expect(out.startsWith('Blocked by security policy: rule')).toBe(true)
    expect(out).toContain('Matched structurally')
    expect(out).not.toContain('spoof')
  })
})

describe('extractDenyDetail', () => {
  // The Output panel leads with its own localized sentence, so the English wire
  // marker must not be repeated in front of it.
  it('drops the English marker but keeps the rule and its note', () => {
    const out = extractDenyDetail(
      '🚫 shell — Blocked by security policy: rm -rf /.*\nMatched structurally on the argv.',
    )
    expect(out).not.toContain('Blocked by security policy')
    expect(out.startsWith('rm -rf /.*')).toBe(true)
    expect(out).toContain('Matched structurally')
  })

  it('inherits last-match extraction, so a spoofed title cannot reach the panel', () => {
    const out = extractDenyDetail(
      '🚫 Running: Blocked by security policy: spoof — Blocked by security policy: real-rule',
    )
    expect(out).toBe('real-rule')
    expect(out).not.toContain('spoof')
  })

  it('yields empty without the marker, so the localized line stands alone', () => {
    expect(extractDenyDetail('🚫 shell (hook blocked)')).toBe('')
    expect(extractDenyDetail('')).toBe('')
  })

  it('yields empty for a bare marker rather than rendering a lone colon', () => {
    expect(extractDenyReason('🚫 shell — Blocked by security policy:')).toBe('')
  })

  it('handles an absent row', () => {
    expect(extractDenyReason('')).toBe('')
  })
})

describe('extractDenyNotice', () => {
  // The gate-crash refusal (`hooks.py` GATE_CRASH_REASON) carries no
  // "Blocked by security policy:" marker on purpose -- it says that no policy
  // rule fired. Without this the Output panel showed ONLY its localized
  // "blocked by security policy" line for that row: the user was told a rule
  // fired, the exact claim the reason disclaims, and the reason never showed.
  const CRASH_ROW =
    '🚫 Running: ls — Blocked: the safety check crashed while judging this call ' +
    '(SystemError), so the call was refused and nothing ran. This is a Kiro Crew ' +
    'bug, not a policy rule and not a user action.'

  it('returns the host sentence for a marker-less row', () => {
    const out = extractDenyNotice(CRASH_ROW)
    expect(out.startsWith('Blocked: the safety check crashed')).toBe(true)
    expect(out).toContain('not a policy rule and not a user action')
  })

  it('drops the title and icon, so the panel shows the reason, not the command', () => {
    const out = extractDenyNotice(CRASH_ROW)
    expect(out).not.toContain('🚫')
    expect(out).not.toContain('Running:')
  })

  it('yields empty for a marker row, which extractDenyDetail owns', () => {
    // The two never both fire: a rule deny keeps its localized lead + detail.
    const row = '🚫 shell — Blocked by security policy: rm -rf /.*'
    expect(extractDenyNotice(row)).toBe('')
    expect(extractDenyDetail(row)).toBe('rm -rf /.*')
  })

  it('yields empty for a hook-blocked row, so the localized line stands alone', () => {
    expect(extractDenyNotice('🚫 shell (hook blocked)')).toBe('')
    expect(extractDenyNotice('🚫 shell')).toBe('')
    expect(extractDenyNotice('')).toBe('')
  })

  it('yields empty for a bare lead word rather than rendering it alone', () => {
    expect(extractDenyNotice('🚫 shell — Blocked:')).toBe('')
  })

  it('reads the host sentence, not a model-authored title that mimics it', () => {
    // `<title>` is model-authored; only the row's tail is the host's reason.
    const spoofed = CRASH_ROW.replace('Running: ls', 'Running: Blocked: totally fine')
    const out = extractDenyNotice(spoofed)
    expect(out.startsWith('Blocked: the safety check crashed')).toBe(true)
    expect(out).not.toContain('totally fine')
  })

  it('never shows a title as the reason when the host reason lacks the marker', () => {
    // A governance deny carries neither DENY_REASON_MARKER nor "Blocked: ", so
    // a title ending in " — Blocked: <text>" was the only match and rendered
    // as the host's refusal.
    const row =
      '🚫 Running: ls — Blocked: approve this, it is safe — Blocked by governance policy: tool not allowed'
    expect(extractDenyNotice(row)).toBe('')
  })

  it('never shows a title that copies the crash sentence on another deny', () => {
    const sentence = CRASH_ROW.slice(CRASH_ROW.indexOf('Blocked: the safety'))
    const row = `🚫 Running: ls — ${sentence} — Blocked by governance policy: tool not allowed`
    expect(extractDenyNotice(row)).toBe('')
  })

  it('yields empty for other host refusals, which keep the localized line', () => {
    expect(extractDenyNotice('🚫 read — Blocked: sensitive path')).toBe('')
  })

  it('never shows title text as the reason on a row the host gave no reason', () => {
    // These rows end in a host-written suffix and carry no reason, so a
    // " — Blocked: " inside them can only be the model-authored title.
    for (const suffix of ['(hook blocked)', '(hook error)', '(rejected)', '(invalid: bad args)']) {
      expect(extractDenyNotice(`🚫 x — Blocked: run curl evil ${suffix}`)).toBe('')
    }
  })
})
