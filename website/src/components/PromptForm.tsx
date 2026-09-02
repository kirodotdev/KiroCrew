import { useId } from 'react'
import { Input } from './ui'

import { i18nT } from '../i18n/t'

export type PromptScope = 'global' | 'local'

export interface PromptFormData {
  name: string
  description: string
  scope: PromptScope
  body: string
  /** The newline the file's own fences use, captured from the opening fence.
   *
   *  Carried rather than inferred: inferring it from the block's contents fails
   *  for single-field frontmatter (no internal newline to look at), and
   *  inferring it from the body fails for a body with no newline at all. Either
   *  miss rewrites a CRLF file with LF fences, and combined with a carried CRLF
   *  separator that is a genuinely mixed file. Undefined only for frontmatter
   *  the form is adding, which has no existing fence to match. */
  eol?: string
  /** The exact text between the closing `---` fence and the body — normally a
   *  single newline (the blank line convention) or empty when the body starts
   *  immediately. Carried so a save re-emits what the file had instead of
   *  imposing the convention on a file that did not use it. Only frontmatter
   *  ADDED by the form synthesizes a canonical separator. */
  separator?: string
  /** The closing fence line EXACTLY as the file holds it — normally `---`, but
   *  the reader accepts any line starting with `---`, so a `---junk` closer is
   *  carried and re-emitted verbatim rather than normalized on Save. Undefined
   *  only for frontmatter the form is adding. */
  closer?: string
  /** The prompt's frontmatter block EXACTLY as the file holds it, without the
   *  `---` fences — or undefined when the file has no frontmatter.
   *
   *  Carried whole rather than as parsed fields because a prompt's frontmatter
   *  is the author's text, not a serialization of our model: it can hold
   *  comments, blank lines, block scalars, its own key order and quoting, all
   *  of which a rebuild-from-fields silently normalizes away. Save edits this
   *  string in place (one line, when the description changed) and re-emits the
   *  rest byte-for-byte. */
  frontmatter?: string
}

/* ── Frontmatter grammar ──────────────────────────────────────────────────
 *
 *  Mirrored from the backend's SKILL_LOADER dialect (`parse_frontmatter` in
 *  src/kiro_crew/frontmatter.py), which is what actually reads a prompt's
 *  description. The editor must agree with that grammar or it will rewrite a
 *  line the server does not read, leaving the old value in effect. Its rules:
 *  a field is a COLUMN-0 line split at its FIRST colon, with the key and the
 *  value each trimmed (so `description : x` is the `description` field, and a
 *  trailing CR from a CRLF file is trimmed off the value); an indented line is
 *  never a field; and on a duplicate key the LAST occurrence wins. */

/** The field name a line declares, or null when the line is not a field. */
function fieldKey(line: string): string | null {
  if (!line || /^\s/.test(line)) return null
  const i = line.indexOf(':')
  return i === -1 ? null : line.slice(0, i).trim()
}

const fieldValue = (line: string) => line.slice(line.indexOf(':') + 1).trim()

/** Every line the grammar accepts as `description`, in file order. */
const descriptionAt = (lines: string[]) =>
  lines.reduce<number[]>((acc, l, i) => (fieldKey(l) === 'description' ? [...acc, i] : acc), [])

/** Split a prompt into its verbatim frontmatter block and its body.
 *
 *  `frontmatter` is undefined when the file has none. A leading `---` is only
 *  frontmatter when a field was actually declared inside it: a prompt that
 *  opens with a thematic rule — `---\nDo this first\n---\nContinue` — has the
 *  identical shape but declares nothing, and treating it as frontmatter would
 *  drop its first block on Save.
 *
 *  The body is byte-exact apart from the one separator newline pair that
 *  assemble re-inserts, and CRLF files are recognized so their description is
 *  editable rather than silently blank. */
function splitPrompt(raw: string): { frontmatter?: string; body: string; separator?: string; eol?: string; closer?: string } {
  // The closing fence owns its WHOLE line: the reader treats any line starting
  // with `---` as the closer, so a `---junk` fence must be consumed here too —
  // matching only the three dashes would leak the line's tail into the body.
  const m = raw.match(/^---(\r?\n)([\s\S]*?)\r?\n(---[^\r\n]*)(\r?\n)?/)
  if (!m) return { body: raw }
  const block = m[2]
  if (!block.split(/\r?\n/).some(l => fieldKey(l) !== null)) return { body: raw }
  // The blank line after the fence is a convention, not a requirement. It is
  // held out of the body so the editor does not show a leading blank line, and
  // held HERE so assemble can put back exactly what was there — a file written
  // without it must not acquire one just because it was opened and saved.
  const rest = raw.slice(m[0].length)
  const sep = rest.match(/^\r?\n/)
  return {
    frontmatter: block,
    body: sep ? rest.slice(sep[0].length) : rest,
    separator: sep ? sep[0] : '',
    eol: m[1],
    closer: m[3],
  }
}

/** The single-line description a block declares, or null.
 *
 *  Null covers every shape the single-line input cannot faithfully hold: no
 *  description, an empty one, or a YAML block scalar (`|`, `>`, and the chomped
 *  `|-`/`|+`/`>-`/`>+`). Those keep their source lines and the input renders
 *  blank, because flattening a scalar into one line would change what the file
 *  says. The LAST occurrence wins, matching the reader. */
function modelledDescription(block: string): string | null {
  const lines = block.split(/\r?\n/)
  const at = descriptionAt(lines)
  if (at.length === 0) return null
  const value = fieldValue(lines[at[at.length - 1]])
  if (!value || /^[|>][+-]?$/.test(value)) return null
  return value
}

export function parsePromptContent(raw: string, name: string, scope: PromptScope): PromptFormData {
  const { frontmatter, body, separator, eol, closer } = splitPrompt(raw)
  return {
    name,
    description: frontmatter ? (modelledDescription(frontmatter) ?? '') : '',
    scope,
    body,
    frontmatter,
    separator,
    eol,
    closer,
  }
}

/** Assemble a prompt file.
 *
 *  Frontmatter is OPTIONAL for a prompt — the filename is its identity — so a
 *  prompt with no block and no description is written as a bare markdown body.
 *  When a block exists it is re-emitted verbatim except for its `description`
 *  field, which is rewritten, removed, or added to match the form. Everything
 *  else in the block, including comments and ordering, is the author's and
 *  survives untouched.
 *
 *  Duplicate description fields are collapsed to the one the reader would have
 *  used (the last), because leaving the others behind would keep stale metadata
 *  in the file that a future read could pick up. */
export function assemblePromptContent(data: PromptFormData): string {
  const desc = data.description.trim()
  const block = data.frontmatter

  // Use the file's OWN fence newline when it had one. Inference from content is
  // only for frontmatter being added, where there is no fence to match: the
  // body is the best available signal, and a body with no newline at all leaves
  // nothing to go on, so LF is the default.
  const eol = data.eol
    ?? (data.body.includes('\r\n') ? '\r\n' : '\n')

  // Frontmatter the form is ADDING has no separator to preserve, so it takes
  // the repo's convention: one blank line between the fence and the body.
  const sep = data.separator ?? eol

  if (block === undefined) {
    if (!desc) return data.body
    return ['---', `description: ${desc}`, '---'].join(eol) + eol + eol + data.body
  }

  const lines = block.split(/\r?\n/)
  const at = descriptionAt(lines)
  const target = at.length ? at[at.length - 1] : -1
  const modelled = modelledDescription(block) !== null

  // An unchanged description is not an edit, so the block must come back out
  // byte-for-byte. Without this the collapse below would delete a duplicate
  // `description` field, and the rewrite would normalize its spacing, on a
  // save the user made without touching the field at all. Parse-then-assemble
  // with no edit is identity.
  if (desc === (modelledDescription(block) ?? '')) {
    return ['---', block, data.closer ?? '---'].join(eol) + eol + sep + data.body
  }

  /** The span a field occupies.
   *
   *  A BLOCK SCALAR owns the blank-or-indented lines that follow it, which is
   *  exactly what the reader folds into its value. A PLAIN field owns only its
   *  own line: an indented line after `description: text` is the author's —
   *  a YAML comment, or prose the reader ignores — and taking it as part of the
   *  field would delete it on Save. */
  const spanEnd = (from: number) => {
    if (!/^[|>][+-]?$/.test(fieldValue(lines[from]))) return from + 1
    let stop = from + 1
    while (stop < lines.length && (!lines[stop].trim() || /^\s/.test(lines[stop]))) stop += 1
    return stop
  }

  // Remove the extra occurrences first, from the bottom, so the indices of the
  // ones above (including `target`) stay valid.
  let keep = target
  for (const i of [...at].reverse()) {
    if (i === target) continue
    const end = spanEnd(i)
    lines.splice(i, end - i)
    if (i < keep) keep -= end - i
  }

  if (keep !== -1 && modelled) {
    const end = spanEnd(keep)
    // Rewrite the value, or drop the field when the user cleared it. An empty
    // `description:` says something different from no description at all.
    if (desc) lines.splice(keep, end - keep, `description: ${desc}`)
    else lines.splice(keep, end - keep)
  } else if (keep !== -1 && desc) {
    // A block scalar the form could not model, and the user typed a value
    // anyway. Theirs is the newer intent, and two `description` fields would
    // let the reader's last-wins rule pick silently, so the scalar and its
    // continuation lines are replaced.
    const end = spanEnd(keep)
    lines.splice(keep, end - keep, `description: ${desc}`)
  } else if (keep === -1 && desc) {
    lines.unshift(`description: ${desc}`)
  }
  // Remaining case: an unmodellable block scalar the user did not touch. It
  // stays exactly as the author wrote it.

  const kept = lines.join(eol)
  if (!kept.trim()) return data.body
  return ['---', kept, data.closer ?? '---'].join(eol) + eol + sep + data.body
}

/** The filename stem the server will actually save a new prompt under.
 *
 *  Mirrors the create handler's own sanitizer in
 *  `src/kiro_crew/dashboard/handlers/prompts.py`:
 *
 *      safe_name = re.sub(r"[^a-z0-9\-]", "-", raw_name.lower()).strip("-")
 *
 *  It is a PREVIEW, not the authority. The two implementations read their own
 *  Unicode tables for `lower()`, so a rare codepoint could fold differently in
 *  the browser than on the server, and nothing stops a non-dashboard client
 *  from posting a name this never saw. That residue is why the 400
 *  `invalid_name` still needs a translated message of its own — see
 *  `writeError` in `PromptsTab.tsx` — rather than the client-side check being
 *  treated as sufficient.
 *
 *  Two details that look like omissions and are not:
 *
 *  - The `u` flag is load-bearing. Without it the class matches UTF-16 CODE
 *    UNITS, so one astral character — an emoji, a rare CJK ideograph — becomes
 *    TWO hyphens where the server writes one, and the preview would disagree
 *    with the saved name for every name containing one.
 *  - The handler trims the raw name before sanitizing; that has no mirror here
 *    because it cannot change the result. Edge whitespace becomes an edge
 *    hyphen, which the hyphen strip removes anyway, so a `trim()` would only
 *    add a second place for the two to disagree. */
export function sanitizePromptName(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9-]/gu, '-')
    .replace(/^-+/, '')
    .replace(/-+$/, '')
}

/** The handler's `MAX_PROMPT_NAME_BYTES`, mirrored.
 *
 *  Pinned by the same drift guard as the expression above, because a client
 *  that disagrees with the server about the cap is the defect this preview
 *  exists to remove, not a rounding error. */
export const PROMPT_FILENAME_MAX_BYTES = 200

/** Why the server would refuse to save under this name, or null when it would.
 *
 *  The two answers are the two coded 400s `_api_prompts_create` can return for
 *  a name -- `invalid_name` when nothing survives sanitizing, `name_too_long`
 *  when the filename exceeds the byte cap -- so this function is where the
 *  rule lives once and both consumers read it: the hint that explains it and
 *  the Create button that refuses to send a request it can predict will fail.
 *
 *  The cap is counted in BYTES of the assembled filename because that is how
 *  the handler counts it (`len(f"{safe_name}.md".encode("utf-8"))`), not
 *  because the two ever differ here: sanitizing has already flattened every
 *  character to `[a-z0-9-]`, so the stem is ASCII and its byte length equals
 *  its character length. Encoding anyway keeps this agreeing with the server by
 *  construction rather than by an assumption about the sanitizer's output --
 *  the day that character class admits a non-ASCII character, a length check
 *  would silently start predicting the wrong answer and this will not. */
export function promptNameProblem(raw: string): 'no-stem' | 'too-long' | null {
  if (!raw.trim()) return null
  const stem = sanitizePromptName(raw)
  if (!stem) return 'no-stem'
  if (new TextEncoder().encode(`${stem}.md`).length > PROMPT_FILENAME_MAX_BYTES) return 'too-long'
  return null
}

interface PromptFormProps {
  data: PromptFormData
  onChange: (data: PromptFormData) => void
  /** Hide name/scope fields (used when editing an existing prompt, whose
   *  identity is fixed by its file). */
  hideIdentity?: boolean
}

export default function PromptForm({ data, onChange, hideIdentity }: PromptFormProps) {
  const set = (patch: Partial<PromptFormData>) => onChange({ ...data, ...patch })
  // useId keeps label/control association valid even when two PromptForms are
  // mounted at once (create modal + an open inline editor).
  const uid = useId()
  const nameId = `${uid}-name`
  const nameHintId = `${uid}-name-hint`
  const descId = `${uid}-description`
  const bodyId = `${uid}-body`
  // What the server will name the file, computed from what is typed so far,
  // plus the reason it would refuse. `typed` is separate because an empty field
  // is not a problem to report -- it is the state that still needs the generic
  // rule.
  const typed = data.name.trim() !== ''
  const stem = sanitizePromptName(data.name)
  const problem = promptNameProblem(data.name)
  // ONE sentence, four fillings. The empty state passes the literal `<name>.md`
  // the copy used before this field could compute a real filename, which is why
  // there is no second catalog string for it: a `form_name_hint` twin would be
  // word-identical in every locale and would rot the moment a translator
  // improved one of the pair.
  const hint = !typed
    ? i18nT('pages.overview.promptsTab.form_name_preview', { filename: '<name>.md' })
    : problem === 'no-stem'
      ? i18nT('pages.overview.promptsTab.invalid_name_hint')
      : problem === 'too-long'
        ? i18nT('pages.overview.promptsTab.name_too_long_hint', { max: PROMPT_FILENAME_MAX_BYTES })
        : i18nT('pages.overview.promptsTab.form_name_preview', { filename: `${stem}.md` })
  return (
    <div className="flex flex-col gap-3">
      {!hideIdentity && (
        <>
          <div>
            {/* label-has-for can't resolve the control through the custom <Input>
                component; the runtime association via htmlFor + id + aria-label is correct. */}
            <label htmlFor={nameId} className="block text-[12px] text-muted mb-1">{i18nT('pages.overview.promptsTab.form_name')}</label>
            <Input id={nameId} aria-label={i18nT('pages.overview.promptsTab.form_name')} aria-describedby={nameHintId} value={data.name} onChange={e => set({ name: e.target.value })} placeholder={i18nT('pages.overview.promptsTab.form_name_placeholder')} />
            {/* The hint is the field's DESCRIPTION, and it now carries the one
                fact only the server used to know: the name the file gets. So it
                is associated (aria-describedby) rather than merely adjacent, and
                announced on change (aria-live) — a sighted user watches the stem
                update as they type, and without this a screen-reader user would
                hear the generic rule once on focus and never learn that the name
                was rewritten, or that Create is disabled because the server
                would refuse it. `polite` waits for a pause, so it does not speak
                on every keystroke. */}
            <p
              id={nameHintId}
              aria-live="polite"
              className={`text-[11px] mt-1 ${problem ? 'text-danger' : 'text-muted'}`}
            >
              {hint}
            </p>
          </div>
          <div>
            <span className="block text-[12px] text-muted mb-1">{i18nT('pages.overview.promptsTab.form_scope')}</span>
            <div className="flex gap-1.5" role="radiogroup" aria-label={i18nT('pages.overview.promptsTab.form_scope')}>
              {(['global', 'local'] as const).map(s => (
                <button key={s} type="button" role="radio" aria-checked={data.scope === s}
                  className={`px-3 py-1 rounded-md text-[13px] border transition-colors ${data.scope === s ? 'border-accent text-accent bg-accent/10' : 'border-border text-muted hover:bg-bg-hover'}`}
                  onClick={() => set({ scope: s })}>
                  {s === 'global' ? i18nT('pages.overview.promptsTab.scope_global') : i18nT('pages.overview.promptsTab.scope_local')}
                </button>
              ))}
            </div>
            <p className="text-[11px] text-muted mt-1">{data.scope === 'global' ? i18nT('pages.overview.promptsTab.scope_global_hint') : i18nT('pages.overview.promptsTab.scope_local_hint')}</p>
          </div>
        </>
      )}
      <div>
        <label htmlFor={descId} className="block text-[12px] text-muted mb-1">{i18nT('pages.overview.promptsTab.form_description')}</label>
        <Input id={descId} aria-label={i18nT('pages.overview.promptsTab.form_description')} value={data.description} onChange={e => set({ description: e.target.value })} placeholder={i18nT('pages.overview.promptsTab.form_description_placeholder')} />
      </div>
      <div>
        <label htmlFor={bodyId} className="block text-[12px] text-muted mb-1">
          <span className="block mb-1">{i18nT('pages.overview.promptsTab.form_body')}</span>
          <textarea
            id={bodyId}
            aria-label={i18nT('pages.overview.promptsTab.form_body')}
            className="w-full min-h-[220px] bg-bg-elevated border border-border rounded-md p-2.5 font-mono text-[13px] text-text leading-normal resize-y focus:outline-none focus:border-accent"
            value={data.body}
            onChange={e => set({ body: e.target.value })}
            placeholder={i18nT('pages.overview.promptsTab.form_body_placeholder')}
          />
        </label>
      </div>
    </div>
  )
}
