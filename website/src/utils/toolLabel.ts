/**
 * Deterministic tool-label language guard.
 *
 * A tool call's "purpose" is model-authored prose (the agent's one-line
 * description of what the call does). The dashboard shows it as the tool-pill
 * label when the user has `simplifiedToolNames` on, and it is persisted
 * verbatim into session history. The agent writes it in whatever UI language
 * was active at the time and only follows the `[UI LANGUAGE]` steer on a
 * best-effort basis, so a transcript can end up holding purposes in a language
 * that no longer matches the user's current interface (e.g. Chinese labels
 * lingering after the user switched the dashboard to English).
 *
 * Rather than trust the model, we decide at RENDER time whether a saved purpose
 * is written in a script compatible with the active UI language. When it is
 * not, callers fall back to the language-neutral raw tool label (the same thing
 * shown when `simplifiedToolNames` is off) so the user never sees
 * foreign-script prose. This is deterministic and history-safe: it corrects old
 * frozen labels and newly-written ones alike, with no dependency on model
 * compliance.
 *
 * SCOPE: the guard operates at the level of WRITING SYSTEM (script), which is
 * all that can be detected reliably from a short string. It catches the jarring
 * cases — CJK / Hangul / Cyrillic / Devanagari / Bengali prose under a
 * Latin-script UI, and vice versa. It cannot distinguish two languages that
 * share the Latin script (e.g. an English purpose under a German UI); those
 * pass through unchanged.
 */

type Script = 'latin' | 'han' | 'kana' | 'hangul' | 'cyrillic' | 'devanagari' | 'bengali'

/** "Hard" (non-Latin) scripts whose mere presence unambiguously signals a
 *  specific language family. Latin is treated as the neutral default and is
 *  not listed here. */
const HARD_SCRIPT_RANGES: Record<Exclude<Script, 'latin'>, RegExp> = {
  han: /[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]/,
  kana: /[\u3040-\u30FF]/,
  hangul: /[\uAC00-\uD7AF\u1100-\u11FF]/,
  cyrillic: /[\u0400-\u04FF]/,
  devanagari: /[\u0900-\u097F]/,
  bengali: /[\u0980-\u09FF]/,
}

/** A Latin letter (ASCII + Latin-1 Supplement / Extended-A accented forms). */
const LATIN_LETTER = /[A-Za-z\u00C0-\u024F]/

/** Expected script(s) for a resolved UI language tag. Matches on the primary
 *  subtag, so `zh-CN`, `zh-TW`, `pt-BR` etc. all resolve correctly. Anything
 *  unknown falls back to Latin, which is the safe default (it only ever
 *  suppresses clearly-foreign hard-script prose). */
function expectedScripts(lang: string): Set<Script> {
  const primary = (lang || '').toLowerCase().split('-')[0]
  switch (primary) {
    case 'zh':
      return new Set<Script>(['han'])
    case 'ja':
      return new Set<Script>(['han', 'kana'])
    case 'ko':
      return new Set<Script>(['hangul'])
    case 'ru':
      return new Set<Script>(['cyrillic'])
    case 'hi':
      return new Set<Script>(['devanagari'])
    case 'bn':
      return new Set<Script>(['bengali'])
    // en, es, fr, pt, de, it, and any unrecognized tag → Latin.
    default:
      return new Set<Script>(['latin'])
  }
}

/**
 * True when `text` is written in a script compatible with the UI language
 * `lang`. Empty / script-neutral text (a bare path, a tool identifier, digits
 * and punctuation only) is always considered compatible — there is nothing to
 * suppress.
 */
export function labelMatchesLanguage(text: string, lang: string): boolean {
  if (!text) return true
  const expected = expectedScripts(lang)

  // Which hard (non-Latin) scripts actually appear in the text?
  const present: Exclude<Script, 'latin'>[] = []
  for (const key of Object.keys(HARD_SCRIPT_RANGES) as Exclude<Script, 'latin'>[]) {
    if (HARD_SCRIPT_RANGES[key].test(text)) present.push(key)
  }

  // Forward mismatch: any hard script present that the UI language does not
  // expect (Han/Hangul/Cyrillic/... prose while the UI is, say, English). A
  // Latin tool identifier mixed into a Chinese purpose still carries Han
  // characters, so a genuine same-language purpose is never suppressed.
  for (const s of present) {
    if (!expected.has(s)) return false
  }

  // Reverse mismatch: a non-Latin UI language expects its own script, so
  // purely Latin/neutral prose (English written while the UI is Chinese) does
  // not match. Only trips when the text actually contains Latin letters, so a
  // script-neutral path/identifier still passes.
  const uiIsNonLatin = !expected.has('latin')
  if (uiIsNonLatin) {
    const hasExpectedScript = present.some(s => expected.has(s))
    if (!hasExpectedScript && LATIN_LETTER.test(text)) return false
  }

  return true
}

/** A label longer than this — or spanning lines — reads better as a derived
 *  summary than as raw code. Threshold only: the collapsed row's CSS `truncate`
 *  (see ToolCallLine's LABEL_COLLAPSED_CLASS) owns visual overflow; this
 *  constant only decides when substituting a summary is worth it. */
export const DERIVE_LABEL_THRESHOLD_CHARS = 200

/** Shell titles arrive as ``Running: <command>`` or as the bare command; MCP
 *  invocations as ``Running: @server/tool``. Only real commands are parseable. */
const RUNNING_PREFIX_RE = /^Running:\s+/
/** ``VAR=value`` prefixes before the actual binary (``FOO=1 cmd …``). */
const ENV_ASSIGN_RE = /^[A-Za-z_]\w*=/
/** Pipeline/chain operators that separate command segments. Bare ``&`` is
 *  excluded: redirects are masked first, and a lone ``&`` tail adds no name. */
const SEGMENT_SPLIT_RE = /\|\|?|&&|;/
/** First redirect target on a line (``> file`` / ``>> file``). */
const REDIRECT_TARGET_RE = />>?\s*([^\s'"&|;]+)/
/** Heredoc opener on a raw (unmasked) line — everything after it is data. */
const HEREDOC_RE = /<<[-~]?\s*['"]?[A-Za-z_]/
/** Shell bookkeeping that says nothing about what a command DOES. Dropped from
 *  the summary when a more meaningful binary is present, kept when it is the
 *  whole command (``cd /tmp`` should still read ``cd``). */
const BOOKKEEPING = new Set(['export', 'cd', 'set', 'source', 'exec', 'unset'])

/** Blank out quoted spans (keeping length) so operators inside quotes do not
 *  split segments — ``grep -E 'foo|bar'`` is one command, not two. Display-only
 *  port of the backend's ``_split_command_segments`` quote masking; escapes are
 *  not interpreted because a wrong guess only degrades a label, never policy. */
function maskQuotes(line: string): string {
  let out = ''
  let quote: string | null = null
  for (const ch of line) {
    if (quote) {
      if (ch === quote) { quote = null; out += ch } else out += ' '
    } else if (ch === "'" || ch === '"') { quote = ch; out += ch } else out += ch
  }
  return out
}

/**
 * Derive a compact, language-neutral summary of a shell command label:
 * the binaries it runs plus the first redirect target.
 *
 *   Running: cat > /tmp/desc.md <<'EOF' …   →  Running: cat → /tmp/desc.md
 *   Running: export P=… cd … ls | grep -i x →  Running: ls, grep
 *
 * A purpose-less shell call falls back to its raw title (`pickToolLabel`), and
 * a raw shell title is the command verbatim. The collapsed row's `truncate`
 * bounds how much of it is VISIBLE; this function makes the visible part
 * MEANINGFUL — clipped raw quoting tells the reader almost nothing.
 *
 * Bare titles (no ``Running:`` prefix) are only parsed when the CALLER has
 * established shell-ness (the tool log's ``is_shell``) — without that gate,
 * ``Editing AGENTS.md`` would "derive" to the binary ``Editing``. Returns null
 * for anything unparseable so the caller keeps the raw label. Built from
 * command names and paths only — script-neutral, no i18n catalog entry needed,
 * and `labelMatchesLanguage` can never suppress it.
 */
export function deriveShellSummary(
  label: string,
  opts: { bareCommand?: boolean } = {},
): string | null {
  const prefix = label.match(RUNNING_PREFIX_RE)
  if (!prefix && !opts.bareCommand) return null
  const cmd = prefix ? label.slice(prefix[0].length) : label
  if (cmd.startsWith('@')) return null
  let names: string[] = []
  let target = ''
  // Parse every line until a heredoc opens: a multi-line script's real work is
  // often not on line 1 (``export PATH=…`` first, ``docker-compose`` second),
  // while everything under a heredoc operator is document body, not commands.
  for (const rawLine of cmd.split('\n')) {
    const masked = maskQuotes(rawLine)
    if (!target) target = masked.match(REDIRECT_TARGET_RE)?.[1] ?? ''
    // Mask redirects AFTER capturing the target so 2>&1 / &> / << neither
    // split segments nor contribute tokens.
    const noRedirects = masked.replace(/\d*>&\d*|&>>?|>>?|<<?[-~]?/g, ' ')
    for (const seg of noRedirects.split(SEGMENT_SPLIT_RE)) {
      const tokens = seg.trim().split(/\s+/).filter(Boolean)
      let i = 0
      while (i < tokens.length && ENV_ASSIGN_RE.test(tokens[i])) i++
      const head = tokens[i]
      if (!head) continue
      const base = head.split('/').pop() || head
      if (/^[\w.@+-]+$/.test(base) && !names.includes(base)) names.push(base)
    }
    if (HEREDOC_RE.test(rawLine)) break
  }
  if (names.length > 1) {
    const meaningful = names.filter(n => !BOOKKEEPING.has(n))
    if (meaningful.length > 0) names = meaningful
  }
  if (names.length === 0) return null
  const shown = names.length > 4 ? `${names.slice(0, 4).join(', ')} …` : names.join(', ')
  return `${prefix ? prefix[0] : ''}${shown}${target ? ` → ${target}` : ''}`
}

/**
 * Choose the text a tool pill / session-row / approval bar should display.
 *
 * - Raw mode (`simplified` off): always the raw tool label.
 * - Simplified mode: the agent's `purpose`, UNLESS its script does not match
 *   the active UI language, in which case fall back to `rawLabel` so the user
 *   never sees prose in a language other than their interface.
 */
export function pickToolLabel(opts: {
  simplified: boolean
  purpose?: string | null
  rawLabel: string
  uiLang: string
}): string {
  const { simplified, purpose, rawLabel, uiLang } = opts
  const trimmed = (purpose ?? '').trim()
  if (!simplified || !trimmed) return rawLabel
  return labelMatchesLanguage(trimmed, uiLang) ? trimmed : rawLabel
}
