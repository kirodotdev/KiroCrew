/**
 * Edition syntax-highlighting languages — the seam that lets a downstream
 * edition add a language to every code surface without editing a core file.
 *
 * The dashboard has two highlighters, and a language has to reach both:
 *   - highlight.js, registered in `hljsLanguages.ts` for BOTH the main-thread
 *     instance and the highlight Web Worker (`hljsWorker.ts`);
 *   - Shiki through Pierre (`@pierre/diffs`), which renders chat code blocks,
 *     diffs, the file viewer and the editor.
 *
 * This cannot be a `register*()` call in the edition's `extensions.tsx` like the
 * other seams: a highlight.js grammar is a function, so it cannot be posted to
 * the worker, and the worker never imports the composition root (which also
 * pulls in React). Instead the edition ships a DATA module,
 * `$KIROCREW_EDITION_DIR/languages.ts`, whose default export is a
 * `HighlightLanguageContribution[]`. The `editionLanguagesPlugin` in
 * vite.config.ts resolves `virtual:kirocrew-edition-languages` to that file
 * (or to an empty list in the stock build), for the main bundle AND the worker
 * bundles. Keep that module worker-safe: no React, no DOM.
 *
 * Core wins every collision. A contribution whose id, alias or extension is
 * already a core language is dropped through `reportSeamCollision` (throws in
 * dev/test, warns in production) by the consumer that owns the conflicting
 * table; this module only rejects contributions that are malformed or collide
 * with each other.
 */
import type { LanguageFn } from 'highlight.js'
import type { LanguageRegistration } from '@pierre/diffs'
import { reportSeamCollision } from '../apps/seamCollision'
import editionLanguages from 'virtual:kirocrew-edition-languages'

export interface HighlightLanguageContribution {
  /** Canonical language id and markdown fence tag, e.g. `demo` for ```demo. */
  id: string
  /** Extra fence tags that resolve to this language. */
  aliases?: string[]
  /** File extensions, with the leading dot, e.g. `.demo`. */
  extensions?: string[]
  /** highlight.js grammar (md-notebook and Mochi code blocks). */
  hljs?: LanguageFn
  /** TextMate grammar(s) for Shiki/Pierre (chat code blocks, diffs, file viewer). */
  textmate?: () => Promise<{ default: LanguageRegistration[] }>
}

/** A contribution after validation: aliases and extensions are always present. */
export interface ResolvedHighlightLanguage extends HighlightLanguageContribution {
  aliases: string[]
  extensions: string[]
}

const SCOPE = 'highlightLanguages'
const NAME_RE = /^[a-z][a-z0-9-]*$/
const EXT_RE = /^\.[a-z0-9][a-z0-9.+-]*$/

const isStringList = (v: unknown): v is string[] =>
  v === undefined || (Array.isArray(v) && v.every(s => typeof s === 'string'))
const isOptionalFn = (v: unknown) => v === undefined || typeof v === 'function'

/** Why an untrusted export entry was rejected. */
interface Rejection {
  reason: string
}

/** Copy one untrusted export entry into a fresh contribution, or return why it is not one. */
function checkShape(c: unknown): HighlightLanguageContribution | Rejection {
  if (typeof c !== 'object' || c === null) return { reason: 'entry is not an object' }
  const e = c as Record<string, unknown>
  const { id, aliases, extensions, hljs, textmate } = e
  if (typeof id !== 'string') return { reason: 'entry has no string id' }
  if (!isStringList(aliases)) return { reason: `language '${id}' aliases must be a string array` }
  if (!isStringList(extensions)) return { reason: `language '${id}' extensions must be a string array` }
  if (!isOptionalFn(hljs) || !isOptionalFn(textmate)) return { reason: `language '${id}' grammars must be functions` }
  return {
    id,
    aliases: aliases && [...aliases],
    extensions: extensions && [...extensions],
    hljs: typeof hljs === 'function' ? (h: Parameters<LanguageFn>[0]) => (hljs as LanguageFn)(h) : undefined,
    textmate:
      typeof textmate === 'function'
        ? () => Promise.resolve().then(() => (textmate as NonNullable<HighlightLanguageContribution['textmate']>)())
        : undefined,
  }
}

/** checkShape, with a throwing getter or Proxy trap reported as a bad entry. */
function readEntry(c: unknown): HighlightLanguageContribution | Rejection {
  try {
    return checkShape(c)
  } catch {
    return { reason: 'entry could not be read' }
  }
}

/**
 * Validate a contribution list. The input is untrusted edition data, so a
 * non-array or a malformed entry is reported and dropped rather than thrown on.
 * Entries with no grammar, and entries whose id/alias/extension repeats an
 * EARLIER contribution, are dropped too; the first registration wins.
 */
export function validateHighlightLanguages(list: unknown): ResolvedHighlightLanguage[] {
  let entries: unknown[] | undefined
  try {
    if (Array.isArray(list)) entries = Array.from(list)
  } catch {
    entries = undefined
  }
  if (entries === undefined) {
    reportSeamCollision(SCOPE, 'the edition languages module must default-export a readable array; ignoring it')
    return []
  }
  const names = new Set<string>()
  const exts = new Set<string>()
  const out: ResolvedHighlightLanguage[] = []
  for (const entry of entries) {
    const c = readEntry(entry)
    if ('reason' in c) {
      reportSeamCollision(SCOPE, `${c.reason}; ignoring`)
      continue
    }
    if (!NAME_RE.test(c.id)) {
      reportSeamCollision(SCOPE, `language id '${c.id}' must match ${NAME_RE}; ignoring`)
      continue
    }
    if (!c.hljs && !c.textmate) {
      reportSeamCollision(SCOPE, `language '${c.id}' supplies neither an hljs nor a textmate grammar; ignoring`)
      continue
    }
    const aliases = [...new Set((c.aliases ?? []).map(a => a.toLowerCase()))].filter(a => a !== c.id)
    const badAlias = aliases.find(a => !NAME_RE.test(a))
    if (badAlias !== undefined) {
      reportSeamCollision(SCOPE, `language '${c.id}' alias '${badAlias}' must match ${NAME_RE}; ignoring the language`)
      continue
    }
    const taken = [c.id, ...aliases].find(n => names.has(n))
    if (taken !== undefined) {
      reportSeamCollision(SCOPE, `language name '${taken}' is already contributed; ignoring '${c.id}'`)
      continue
    }
    const extensions: string[] = []
    for (const raw of c.extensions ?? []) {
      const ext = raw.toLowerCase()
      if (!EXT_RE.test(ext)) {
        reportSeamCollision(SCOPE, `language '${c.id}' extension '${raw}' must start with a dot; ignoring the extension`)
        continue
      }
      if (exts.has(ext)) {
        reportSeamCollision(SCOPE, `extension '${ext}' is already contributed; ignoring it for '${c.id}'`)
        continue
      }
      exts.add(ext)
      extensions.push(ext)
    }
    names.add(c.id)
    for (const a of aliases) names.add(a)
    out.push({ ...c, aliases, extensions })
  }
  return out
}

/** The edition's validated contributions. Empty in the stock build. */
export const HIGHLIGHT_LANGUAGES: readonly ResolvedHighlightLanguage[] = Object.freeze(
  validateHighlightLanguages(editionLanguages),
)

/** The contributed language id for a fence tag (id or alias), if any. */
export function highlightLanguageForTag(
  tag: string,
  languages: readonly ResolvedHighlightLanguage[] = HIGHLIGHT_LANGUAGES,
): ResolvedHighlightLanguage | undefined {
  const t = tag.toLowerCase()
  return languages.find(l => l.id === t || l.aliases.includes(t))
}

/** The contributed language for a file extension (with the dot), if any. */
export function highlightLanguageForExtension(
  ext: string,
  languages: readonly ResolvedHighlightLanguage[] = HIGHLIGHT_LANGUAGES,
): ResolvedHighlightLanguage | undefined {
  const e = ext.toLowerCase()
  return languages.find(l => l.extensions.includes(e))
}
