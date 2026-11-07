/**
 * Lay a shell command out for the tool pill's hover bubble: one pipeline stage
 * per line, each line led by the operator that joins it to the one before
 * (`|`, `&&`, `||`, `;`), and every word classified so the bubble can colour
 * the program, its flags, quoted strings and redirections apart.
 *
 * This is DISPLAY ONLY. The bubble also carries the verbatim command for
 * assistive tech, and the expanded row still shows the exact payload, so the
 * tokenizer may be approximate: it is quote-aware (single, double, backslash)
 * and treats `$(...)`, `<(...)` and backticks as opaque, which is what keeps a
 * `|` inside `"a|b"` or `$(ls | wc -l)` from splitting a line. A physical
 * newline starts a new line in the same layout, keeping its indentation; a
 * trailing `\` joins the next line to it, and a heredoc body is shown as plain
 * text, whitespace and blank lines kept, up to its delimiter. A line that ends
 * inside an open quote or substitution returns `null`, so the caller shows the
 * command verbatim.
 */

export type ShellPieceKind = 'program' | 'flag' | 'string' | 'redirect' | 'text'
export interface ShellPiece { kind: ShellPieceKind; text: string }
/** One rendered line: the operator that joins it to the previous line (empty
 *  on the first), then its pieces. Whitespace between words is a `text` piece. */
export interface ShellLine { op: string; pieces: ShellPiece[] }

/** Operators that start a new line. A lone `&` (background) stays inline. */
const LINE_OPS = ['&&', '||', '|&', '|', ';'] as const

interface Word { parts: { quoted: boolean; text: string }[]; raw: string }
type Tok = { t: 'word'; w: Word } | { t: 'space'; text: string } | { t: 'op'; text: string } | { t: 'redir'; text: string }

function tokenize(src: string): { toks: Tok[]; open: boolean } {
  const toks: Tok[] = []
  let open = false
  let word: Word | null = null
  let i = 0
  const push = (quoted: boolean, text: string) => {
    if (!word) word = { parts: [], raw: '' }
    const last = word.parts[word.parts.length - 1]
    if (last && last.quoted === quoted) last.text += text
    else word.parts.push({ quoted, text })
    word.raw += text
  }
  const endWord = () => { if (word) { toks.push({ t: 'word', w: word }); word = null } }
  while (i < src.length) {
    const c = src[i]
    if (c === ' ' || c === '\t') {
      endWord()
      let j = i
      while (j < src.length && (src[j] === ' ' || src[j] === '\t')) j++
      toks.push({ t: 'space', text: src.slice(i, j) })
      i = j
      continue
    }
    if (c === '\\') { push(false, src.slice(i, i + 2)); i += 2; continue }
    if (c === "'" || c === '"') {
      // Unterminated quote: the rest of the command is the string.
      let j = i + 1
      while (j < src.length && src[j] !== c) j += (c === '"' && src[j] === '\\') ? 2 : 1
      if (j >= src.length) open = true
      push(true, src.slice(i, Math.min(j + 1, src.length)))
      i = j + 1
      continue
    }
    if (c === '`' || ((c === '$' || c === '<' || c === '>') && src[i + 1] === '(')) {
      // Opaque substitution: copied whole, so nothing inside splits the line.
      let j: number
      if (c === '`') {
        j = src.indexOf('`', i + 1)
        if (j < 0) open = true
        j = j < 0 ? src.length : j + 1
      } else {
        let depth = 0
        j = i + 1
        for (; j < src.length; j++) {
          const d = src[j]
          if (d === '\\') { j++; continue }
          if (d === "'" || d === '"') { const k = src.indexOf(d, j + 1); j = k < 0 ? src.length : k; continue }
          if (d === '(') depth++
          else if (d === ')' && --depth === 0) { j++; break }
        }
        if (depth > 0) open = true
      }
      push(false, src.slice(i, j))
      i = j
      continue
    }
    const op = LINE_OPS.find(o => src.startsWith(o, i))
    if (op) { endWord(); toks.push({ t: 'op', text: op }); i += op.length; continue }
    if (c === '>' || c === '<' || (c === '&' && src[i + 1] === '>')) {
      // A redirection, with its fd prefix (`2>`) when the word so far is digits.
      let prefix = ''
      const w = word as Word | null
      if (w && /^\d+$/.test(w.raw)) { prefix = w.raw; word = null }
      endWord()
      // `>|` ahead of `>>?`: matched as `>` it would leave the `|` to split
      // the line, and the target file would read as a new stage's program.
      const m = /^(&>>?|>\||>>?|<<<|<<-?|<)(&(\d+|-))?/.exec(src.slice(i))
      const text = m ? m[0] : c
      toks.push({ t: 'redir', text: prefix + text })
      i += text.length
      continue
    }
    push(false, c)
    i++
  }
  endWord()
  return { toks, open }
}

/** `FOO=bar` before the program is an environment assignment, not the program. */
const ENV_ASSIGN = /^[A-Za-z_][A-Za-z0-9_]*=/

/** A heredoc delimiter as the shell compares it: quotes and backslashes removed. */
function heredocDelimiter(w: Word): string {
  return w.parts.map(p => (p.quoted ? p.text.slice(1, -1) : p.text.replace(/\\/g, ''))).join('')
}

/** True when *text* ends in a backslash that escapes the newline after it. */
function continues(text: string): boolean {
  const m = /\\+$/.exec(text)
  return m != null && m[0].length % 2 === 1
}

export function formatShellForTip(command: string): ShellLine[] | null {
  const src = command.trim()
  if (!src) return null
  const physical = src.split(/\r?\n/)
  const lines: ShellLine[] = []
  // Rows kept as typed: heredoc body and blank lines are never trimmed away.
  const verbatim = new Set<ShellLine>()
  const pending: { delim: string; tabs: boolean }[] = []
  let sawProgram = false
  const row = (op: string): ShellLine => {
    const line: ShellLine = { op, pieces: [] }
    lines.push(line)
    sawProgram = false
    return line
  }
  for (let n = 0; n < physical.length; n++) {
    let text = physical[n]
    if (pending.length) {
      const body = row('')
      if (text) body.pieces.push({ kind: 'text', text })
      verbatim.add(body)
      const { delim, tabs } = pending[0]
      if ((tabs ? text.replace(/^\t+/, '') : text) === delim) pending.shift()
      continue
    }
    while (continues(text) && n + 1 < physical.length) text = text.slice(0, -1) + physical[++n]
    if (!text.trim()) {
      verbatim.add(row(''))
      continue
    }
    const { toks, open } = tokenize(text)
    if (open) return null
    // A line that ended on an operator (`ls |`, `npm ci &&`) left its row
    // open; the next physical line fills it, so the chain keeps its operator.
    const last = lines[lines.length - 1]
    const joins = last != null && last.op !== '' && last.pieces.length === 0 && !verbatim.has(last)
    let line = joins ? last : row('')
    // Indentation that starts a physical line is the script's own; whitespace
    // after an operator is the gutter's job.
    let lead = !joins
    for (let k = 0; k < toks.length; k++) {
      const tok = toks[k]
      if (tok.t === 'op') {
        line = row(tok.text)
        lead = false
        continue
      }
      if (tok.t === 'space') {
        if (line.pieces.length || lead) line.pieces.push({ kind: 'text', text: tok.text })
        continue
      }
      lead = false
      if (tok.t === 'redir') {
        line.pieces.push({ kind: 'redirect', text: tok.text })
        if (tok.text === '<<' || tok.text === '<<-') {
          const next = toks.slice(k + 1).find(t => t.t !== 'space')
          if (next?.t === 'word') pending.push({ delim: heredocDelimiter(next.w), tabs: tok.text === '<<-' })
        }
        continue
      }
      const { w } = tok
      let kind: ShellPieceKind = 'text'
      if (!sawProgram && !ENV_ASSIGN.test(w.raw)) { kind = 'program'; sawProgram = true }
      else if (/^-/.test(w.raw)) kind = 'flag'
      for (const part of w.parts) line.pieces.push({ kind: part.quoted ? 'string' : kind, text: part.text })
    }
  }
  for (const line of lines) {
    if (verbatim.has(line)) continue
    while (line.pieces.length && line.pieces[line.pieces.length - 1].kind === 'text' && !line.pieces[line.pieces.length - 1].text.trim()) line.pieces.pop()
  }
  // A trailing `;` leaves an empty last line; it is noise in the gutter.
  return lines.filter((l, idx) => idx === 0 || l.pieces.length > 0 || verbatim.has(l))
}
