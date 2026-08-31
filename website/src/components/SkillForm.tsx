import { useState } from 'react'
import { Document, isAlias, isMap, isScalar, isSeq, parseDocument, Scalar, visit } from 'yaml'
import type { Node, Pair, YAMLSeq } from 'yaml'
import { Input } from './ui'

import { i18nT } from '../i18n/t'
export interface SkillFormData {
  name: string
  category: string
  description: string
  triggers: string
  tags: string
  always: boolean
  body: string
  /** The skill's ORIGINAL frontmatter block, carried verbatim. A save parses it,
   *  replaces the source range of each field the form owns, and copies every
   *  other byte through — see `assembleSkillContent`. */
  frontmatter?: string
  /** Raw markdown content (frontmatter + body). Used in raw editing mode. */
  raw?: string
}

interface SkillFormProps {
  data: SkillFormData
  onChange: (data: SkillFormData) => void
  /** Hide name/category fields (used when editing an existing skill) */
  hideIdentity?: boolean
  /** Show raw mode toggle (default: true) */
  allowRaw?: boolean
}

/** Frontmatter keys the structured form owns. Every other key — and every
 *  comment, blank line and orphan line between them — is carried through as its
 *  original bytes. */
const MANAGED_KEYS = new Set(['name', 'description', 'always', 'triggers', 'tags'])

/** The order a field is written in when the file does not already declare it, so
 *  a new skill and an edited one agree. */
const MANAGED_ORDER = ['name', 'description', 'always', 'triggers', 'tags'] as const

/** Locate the frontmatter block. The `---` fences are markdown, not YAML, so
 *  finding them is not the parser's job. `block` runs up to the newline that
 *  opens the closing fence, so it carries a trailing newline exactly when the
 *  author left a blank line there — which is preserved rather than trimmed. */
function splitBlock(raw: string): { block: string; body: string } | null {
  if (!raw.startsWith('---')) return null
  const end = raw.indexOf('\n---', 3)
  if (end === -1) return null
  return { block: raw.slice(4, end), body: raw.slice(end + 4).trim() }
}

/** Parse a block for READING. Deliberately tolerant: a file with a duplicate key
 *  or a stray tab still has fields worth showing in the meta strip, and a display
 *  surface cannot corrupt anything. Writing uses `isRewritable` instead. */
function readBlock(block: string): Document.Parsed | null {
  const doc = parseDocument(block)
  return isMap(doc.contents) ? doc : null
}

/** Whether the block uses a YAML anchor or alias anywhere.
 *
 *  A managed field can carry the anchor an unmodelled field aliases
 *  (`description: &shared old` with `note: *shared`). Re-rendering that field
 *  drops the anchor while the alias is copied through, and the saved file no
 *  longer parses. Rather than teach the writer to carry anchors, the whole block
 *  is treated as not spliceable and edited raw -- the same answer this code gives
 *  every other construct it cannot rewrite safely. The test is deliberately
 *  whole-block rather than per-field: an anchor on a field nobody models would
 *  survive, but a rule that holds for the entire block is the one worth reasoning
 *  about for a construct that essentially never appears in skill frontmatter. */
function hasAnchorOrAlias(doc: Document.Parsed): boolean {
  let found = false
  visit(doc, (_key, node) => {
    if (isAlias(node) || (node as { anchor?: string } | null)?.anchor) {
      found = true
      return visit.BREAK
    }
    return undefined
  })
  return found
}

/** Whether every top-level key starts at column 0.
 *
 *  One rule covers the two layouts the splice cannot rewrite faithfully:
 *
 *  - an EXPLICIT key (`? name` on one line, `: value` on the next) puts `? `
 *    before the key, so replacing the key's own range leaves that marker behind;
 *  - a ROOT-INDENTED mapping (every key indented by the same amount) is valid
 *    YAML, but a field appended at column 0 would then sit at a different
 *    indentation from its siblings, which is a YAML error rather than a cosmetic
 *    difference.
 *
 *  Both are valid input, so neither may be rewritten on a guess; both fall back to
 *  the raw editor. Checking the key's column subsumes both without having to
 *  detect either construct by name. */
function everyTopKeyAtColumnZero(block: string, doc: Document.Parsed): boolean {
  if (!isMap(doc.contents)) return false
  for (const pair of doc.contents.items as Pair<unknown, unknown>[]) {
    const range = isScalar(pair.key) ? pair.key.range : null
    if (!range) return false
    const lineStart = block.lastIndexOf('\n', range[0] - 1) + 1
    if (block.slice(lineStart, range[0]).length > 0) return false
  }
  return true
}

/** Reproduce the reader's fold for a BARE LITERAL block scalar, from the source lines
 *  after its header.
 *
 *  Mirrors `fold_block_scalar` in `src/kiro_crew/frontmatter.py` for the `|` family only:
 *  it drops trailing blank lines, dedents by the first non-blank line's indent, joins with
 *  newlines and strips. The strip is the part no YAML chomping mode performs, and it is
 *  why comparing beats predicting -- a leading blank line survives in the parser and not
 *  here, and nothing about the indicator says so. */
function backendFoldsLiteral(block: string, headerEnd: number): string {
  const rest = block.slice(headerEnd + 1).split('\n')
  const body: string[] = []
  for (const line of rest) {
    // The scalar ends at the first non-blank line that is not indented.
    if (line.trim() !== '' && !/^\s/.test(line)) break
    body.push(line)
  }
  let end = body.length
  while (end && body[end - 1].trim() === '') end -= 1
  if (!end) return ''
  const kept = body.slice(0, end)
  const first = kept.find(l => l.trim() !== '') ?? ''
  const indent = first.length - first.replace(/^\s+/, '').length
  const dedented = kept.map(l => (l.slice(0, indent).trim() === '' ? l.slice(indent) : l.replace(/^\s+/, '')))
  return dedented.join('\n').trim()
}

/** What the BACKEND reader would take a managed field's value to be, from its own
 *  source line. Returns null when the shape is one both readers agree on and so needs
 *  no comparison.
 *
 *  `SKILL_LOADER` reads a column-0 `key: value` line, takes the rest of the line, and
 *  strips quote characters off both ends. It never unescapes, and it never continues a
 *  plain scalar onto the next line. Block scalars are excluded here: the backend
 *  implements the same folding the parser does, so the two agree by construction. */
function backendReadsValue(block: string, pair: Pair<unknown, unknown>): string | null {
  const keyRange = isScalar(pair.key) ? pair.key.range : null
  if (!keyRange) return null
  const lineStart = block.lastIndexOf('\n', keyRange[0] - 1) + 1
  let lineEnd = block.indexOf('\n', lineStart)
  if (lineEnd === -1) lineEnd = block.length
  const line = block.slice(lineStart, lineEnd)
  const colon = line.indexOf(':')
  if (colon === -1) return null
  const rhs = line.slice(colon + 1).trim()
  /* Block scalars. Three attempts at deciding agreement from the INDICATOR were all
     wrong -- six (the reader's resolvable set), then four, then the realisation that the
     reader's fold ends in `.strip()`, which eats LEADING whitespace too, so
     `always: |-` with a blank first line reads `true` on the backend and
     newline-then-true in the parser. Agreement depends on the CONTENT.
     So stop predicting and SIMULATE, exactly as the single-line branch below does.
     For a bare LITERAL indicator the reader's fold is short enough to reproduce
     faithfully: drop trailing blank lines, dedent by the first non-blank line's indent,
     join with newlines, strip. Verified against the real `fold_block_scalar`.
     A FOLDED (`>`) form is NOT reproduced -- its blank-line and indentation folding rules
     are intricate, and duplicating them is the cross-language coupling this change exists
     to avoid -- so it falls through and is refused, as does an explicit indicator. */
  if (/^\|[+-]?$/.test(rhs)) return backendFoldsLiteral(block, lineEnd)
  return rhs.replace(/^["']+/, '').replace(/["']+$/, '')
}

/** Whether the BACKEND would read a managed field differently than YAML decodes it.
 *
 *  The writer already refuses to EMIT a form the reader cannot decode. This is the
 *  mirror of that on the READ side, and it closes a divergence this change introduced:
 *  main read frontmatter with the same line dialect it wrote with, so its idea of a
 *  value always matched the backend's. Reading with a real YAML parser is what makes
 *  them disagree -- `description: "first\nsecond"` is one newline to the parser and the
 *  two characters backslash-n to the backend.
 *
 *  Adopting the parser's reading and saving it would silently redefine what the file
 *  means to the code that loads skills, without the author asking for it. Where the two
 *  disagree, the block is edited raw instead, so the form only ever rewrites fields
 *  whose meaning both sides agree on. */
function backendDisagreesOnManagedField(block: string, doc: Document.Parsed): boolean {
  if (!isMap(doc.contents)) return false
  for (const pair of doc.contents.items as Pair<unknown, unknown>[]) {
    if (!MANAGED_KEYS.has(pairKey(pair))) continue
    if (!isScalar(pair.value)) continue
    /* A comment on the field's own line is `managedFieldCarriesComment`'s case, and it
       refuses the block already. Skipping it here keeps one concern per rule and lets
       the decline message still name the comment as the cause -- and the backend does
       NOT strip a trailing comment, so a naive comparison would fire on every
       commented field and mask that reason. */
    if (typeof (pair.value as { comment?: unknown }).comment === 'string') continue
    const backend = backendReadsValue(block, pair)
    if (backend === null) continue
    if (backend !== scalarText(pair.value)) return true
  }
  return false
}

/** Whether any MANAGED field shares its line with a comment.
 *
 *  Four review rounds each found a different way that weaving a new value back
 *  into a line the author's comment also occupies goes wrong: an inline comment
 *  discarded on drop, a block-scalar header comment discarded on replace and on
 *  drop, and a trailing comment absorbed into the value when an edit turned it
 *  multi-line. Each was fixed with its own branch, and the fourth fix then emitted
 *  `description: |- # note` -- a form the BACKEND reader treats as literal text,
 *  discarding the whole value. Weaving is the wrong shape: every arrangement of
 *  value and comment on one line is a separate case, which is the same
 *  unfinishable enumeration this change exists to replace.
 *
 *  So the splice declines instead, and the block is edited raw -- the answer this
 *  code already gives anchors, explicit keys and root-indented mappings. Nothing
 *  is destroyed either way, which is the guarantee `#1825` actually asks for.
 *
 *  `node.comment` covers both arrangements: a block scalar's header comment and a
 *  plain value's trailing comment both land there (measured). A comment on the line
 *  ABOVE a key is `commentBefore`, which the splice never touches, so it is not
 *  included here. */
function managedFieldCarriesComment(doc: Document.Parsed): boolean {
  if (!isMap(doc.contents)) return false
  for (const pair of doc.contents.items as Pair<unknown, unknown>[]) {
    if (!MANAGED_KEYS.has(pairKey(pair))) continue
    // A comment after the key itself, before the colon.
    if (typeof (pair.key as { comment?: unknown } | null)?.comment === 'string') return true
    // Anywhere in the VALUE's subtree. The root is not enough: editing Tags
    // replaces the whole sequence, so a comment on one ITEM goes with it, and a
    // comment above an item is `commentBefore` on that item rather than on the
    // sequence. Deliberately NOT the key's `commentBefore` -- a comment on the line
    // ABOVE a key sits outside every range the splice touches and survives.
    const vnode = pair.value
    if (!vnode || typeof vnode !== 'object') continue
    let found = false
    visit(vnode as Node, (_key, node) => {
      const n = node as { comment?: unknown; commentBefore?: unknown } | null
      if (typeof n?.comment === 'string' || typeof n?.commentBefore === 'string') {
        found = true
        return visit.BREAK
      }
      return undefined
    })
    if (found) return true
  }
  return false
}

/** Whether a managed LIST field survives the form's own round-trip.
 *
 *  `triggers` and `tags` are ONE single-line text input holding a comma-separated
 *  list, and YAML gives that field two legitimate shapes -- a sequence, or a scalar
 *  the editor itself writes as `alpha, beta`. The requirement is the same for both
 *  (come back unchanged from what that input can carry) but it lands differently on
 *  each, and the difference is the reason this is one function:
 *
 *  - SCALAR: only newlines are fatal. A single-line input cannot hold one, so the
 *    browser strips it and a block-literal list merges into one entry. Commas here
 *    are the field's OWN separator and round-trip by design.
 *  - SEQUENCE: read joins the items with `', '`; save splits on `,`, trims each piece
 *    and drops the empties. So an item must additionally be non-empty, carry no comma
 *    (the separator, or one item becomes two), and equal its own trimmed text.
 *
 *  Written as the round-trip requirement rather than a list of rejected shapes.
 *  The DEFAULT IS DENY, and that is the substance of the rule rather than a detail.
 *  Five earlier versions were "allow unless I recognise a problem", and every one of
 *  them shipped a hole where an unrecognised node kind fell out of the bottom: non-scalar
 *  items, then empty items, then multiline items, then multiline SCALARS, then a MAPPING
 *  value. The kinds this field CAN represent are exactly three -- absent, a single-line
 *  scalar, a sequence of single-line scalars -- so those are named and everything else is
 *  refused, including any node kind a future YAML version introduces.
 *
 *  The question is never "which shapes are bad" but "what can this input carry and give
 *  back". */
function managedListIsRepresentable(vnode: unknown): boolean {
  // Absent value (`tags:` with nothing after it). Spliceable: it is what the editor
  // writes for an empty field, and an earlier round fixed it being dropped outright.
  if (vnode === null || vnode === undefined) return true
  if (isScalar(vnode)) {
    const text = vnode.value
    return typeof text !== 'string' || !/[\r\n]/.test(text)
  }
  if (isSeq(vnode)) {
    for (const item of vnode.items) {
      if (!isScalar(item)) return false
      const text = item.value
      if (typeof text !== 'string') return false
      if (text === '' || text !== text.trim()) return false
      if (text.includes(',') || /[\r\n]/.test(text)) return false
    }
    return true
  }
  // A mapping, or any other node kind: no representation in a comma-separated input.
  return false
}

/** Whether any managed list field holds a value the form cannot represent. */
function managedListUnrepresentable(doc: Document.Parsed): boolean {
  if (!isMap(doc.contents)) return false
  for (const pair of doc.contents.items as Pair<unknown, unknown>[]) {
    const key = pairKey(pair)
    if (key !== 'tags' && key !== 'triggers') continue
    if (!managedListIsRepresentable(pair.value)) return true
  }
  return false
}

/** Whether a save may splice this document's source. Requires YAML the parser
 *  accepted completely and a block mapping root — the two things that make the
 *  reported source ranges trustworthy. A flow mapping (`{a: 1}`) is a mapping but
 *  has no per-line structure to splice, so it is excluded, and so are anchors,
 *  aliases, any mapping layout whose keys are not at column 0, any block whose
 *  managed fields share a line with a comment, and any managed list holding an item
 *  the form's comma-separated input cannot round-trip. Anything rejected here is
 *  edited RAW instead, because a form cannot safely rewrite what it cannot
 *  represent. */
/** Whether the block carries a YAML document-end marker.
 *
 *  `...` at column 0 ends the document. The parser drops it from the AST -- anything
 *  after it belongs to a second document `parseDocument` never returns -- so no
 *  AST-level rule sees it, and an append (the path a MISSING managed field takes)
 *  lands after the marker where the reader will never look. Measured: adding `tags`
 *  to a block ending in `...` put the new key one line below it.
 *
 *  Refused rather than taught to insert before the marker. Placing added keys
 *  correctly around it means re-deriving an insertion point from a construct the AST
 *  does not carry, which is the line-arithmetic this change exists to remove. */
function hasDocumentEndMarker(block: string): boolean {
  return block.split('\n').some(line => /^\.\.\.(\s|$)/.test(line))
}

/** Every reason a block cannot be spliced, in one place.
 *
 *  One list, two questions: `isRewritable` asks whether ANY rule refuses, and
 *  `commentIsTheOnlyObstacle` asks whether the comment rule is the ONLY one that does.
 *  They used to be two hand-written chains differing by a single line, which meant a
 *  new rule had to be added to both -- I added `backendDisagreesOnManagedField` to each
 *  by hand one round ago, and First Principles was right that the next one would be
 *  forgotten in whichever chain was not in front of me. Deriving both answers from this
 *  list makes that class of mistake impossible rather than merely unlikely. */
const REFUSAL_RULES: { id: 'comment' | 'other'; refuses: (block: string, doc: Document.Parsed) => boolean }[] = [
  { id: 'other', refuses: (_b, doc) => doc.errors.length > 0 },
  { id: 'other', refuses: (_b, doc) => !isMap(doc.contents) || !!doc.contents.flow },
  { id: 'other', refuses: block => hasDocumentEndMarker(block) },
  { id: 'other', refuses: (_b, doc) => hasAnchorOrAlias(doc) },
  { id: 'other', refuses: (block, doc) => backendDisagreesOnManagedField(block, doc) },
  { id: 'comment', refuses: (_b, doc) => managedFieldCarriesComment(doc) },
  { id: 'other', refuses: (_b, doc) => managedListUnrepresentable(doc) },
  { id: 'other', refuses: (block, doc) => !everyTopKeyAtColumnZero(block, doc) },
]

function isRewritable(block: string, doc: Document.Parsed | null): boolean {
  if (!doc) return false
  return !REFUSAL_RULES.some(rule => rule.refuses(block, doc))
}

/** Whether an inline comment is the ONLY thing standing between this block and the
 *  structured editor.
 *
 *  Drives a message that tells the user which comment to move, so the control is
 *  recoverable instead of permanently mysterious (UX review). It requires every OTHER
 *  rule to pass: a block refused for a second reason as well would send the user to
 *  delete a comment and leave them facing the same raw editor, which is worse than
 *  saying nothing specific. */
export function commentIsTheOnlyObstacle(raw: string): boolean {
  const split = splitBlock(raw)
  if (!split || !split.block.trim()) return false
  const doc = readBlock(split.block)
  if (!doc) return false
  const refusing = REFUSAL_RULES.filter(rule => rule.refuses(split.block, doc))
  return refusing.length === 1 && refusing[0].id === 'comment'
}

/** A scalar as the text the form and the meta strip show. A block scalar's value
 *  carries the trailing newline its clip chomping implies; the form's textarea
 *  does not, and re-adding it on every save would grow the file a byte at a
 *  time. */
function scalarText(node: Scalar): string {
  if (node.value == null) return ''
  if (typeof node.value !== 'string') return String(node.value)
  return node.type === 'BLOCK_LITERAL' || node.type === 'BLOCK_FOLDED'
    ? node.value.replace(/\n$/, '')
    : node.value
}

/** A node as a single line of text. `triggers` and `tags` are comma lists in the
 *  UI but may legitimately be written as YAML sequences, so a sequence is joined
 *  rather than shown as source. A mapping has no one-line form; its source is at
 *  least truthful. */
function nodeText(node: unknown, block: string): string {
  if (node == null) return ''
  if (isScalar(node)) return scalarText(node)
  if (isSeq(node)) {
    return node.items.map(item => (isScalar(item) ? scalarText(item) : '')).filter(Boolean).join(', ')
  }
  const range = (node as Node).range
  return range ? block.slice(range[0], range[1]).trim() : ''
}

function pairKey(pair: Pair<unknown, unknown>): string {
  return isScalar(pair.key) ? String(pair.key.value ?? '') : ''
}

/** Every top-level key of a parsed block, flattened to text. */
function metaOf(doc: Document.Parsed, block: string): Record<string, string> {
  const meta: Record<string, string> = {}
  if (!isMap(doc.contents)) return meta
  for (const pair of doc.contents.items as Pair<unknown, unknown>[]) {
    const key = pairKey(pair)
    if (key) meta[key] = nodeText(pair.value, block)
  }
  return meta
}

/** Parse YAML frontmatter from raw skill content. Shared by SkillsTab (display),
 *  SkillBrowserModal, SkillDirectoryBrowser and SkillForm (edit).
 *
 *  `meta` is a flattened text view for reading fields and rendering meta strips.
 *  It is NOT the save path: a save re-parses the original block and splices it,
 *  so nothing here has to be lossless. */
export function parseFrontmatter(raw: string): {
  meta: Record<string, string>
  body: string
} {
  const split = splitBlock(raw)
  if (!split) return { meta: {}, body: raw }
  const doc = readBlock(split.block)
  return { meta: doc ? metaOf(doc, split.block) : {}, body: split.body }
}

/** The form's value for a managed key, as the text that key holds in the file.
 *  Comparing this against the parsed value is how an untouched field is detected
 *  and left byte-identical. */
function managedText(key: string, data: SkillFormData): string {
  if (key === 'name') return data.name
  if (key === 'description') return data.description
  if (key === 'always') return data.always ? 'true' : ''
  if (key === 'triggers') return data.triggers
  return data.tags
}

/** Whether the form's value for a managed key still says what the file says, in
 *  which case the field is copied verbatim rather than re-rendered.
 *
 *  `always` needs its own comparison, and it must be an INVERSION rather than a
 *  list of false-like spellings. The form reads the flag as `meta.always ===
 *  'true'`, so the field is unchanged exactly when the checkbox still agrees with
 *  that same test applied to the original text. Enumerating `''` and `'false'`
 *  missed every other non-`true` spelling -- `always: yes` and `always: no` parse
 *  as STRINGS under the YAML 1.2 core schema, `always: 0` as a number -- and each
 *  one was silently deleted on an unrelated edit. Enumerating shapes is the exact
 *  mistake this whole change exists to stop making. */
function managedUnchanged(key: string, data: SkillFormData, original: Record<string, string>): boolean {
  const was = original[key] ?? ''
  if (key === 'always') return data.always === (was === 'true')
  return managedText(key, data) === was
}

/** Characters YAML cannot carry in scalar content at all: the C0 controls except
 *  tab and newline, plus DEL and the C1 range. A value holding one has NO
 *  representation this backend reads -- a double-quoted escape is the only YAML form,
 *  and the reader does not unescape, so it would corrupt the whole value. Measured:
 *  asking `yaml` for a block scalar on such a value is overridden and the escaped
 *  form comes back anyway, so there is nothing to choose between.
 *
 *  Drop them, and fold a carriage return into a newline. That loses an invisible
 *  character the file could not legally have held, instead of losing the whole
 *  field, and the newline folding matches what an HTML control does to its own value
 *  before the form ever sees it. */
const UNREPRESENTABLE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g

function representable(value: string): string {
  return value.replace(/\r\n?/g, '\n').replace(UNREPRESENTABLE, '')
}

/** Emit `key: value` as a bare block literal, or null when the library refuses that
 *  style. A lone carriage return or a control character has no block representation,
 *  so `yaml` overrides the requested style and re-emits the escaped form. */
function asBlockLiteral(key: string, value: string): string | null {
  const doc = new Document({})
  const node = doc.createNode(value) as Scalar
  node.type = Scalar.BLOCK_LITERAL
  doc.set(key, node)
  const text = doc.toString(RENDER_OPTIONS).replace(/\n$/, '')
  return /:\s[|>][+-]?(\s|$)/.test(text.split('\n')[0] ?? '') ? text : null
}

/* `flowCollectionPadding: false` keeps `tags: [a, b]`, the exact spacing this editor
   has always written. The library pads to `[ a, b ]` by default, which would rewrite
   that line in every file anyone edits -- a gratuitous byte change in a change whose
   whole point is not making them (First Principles review). */
const RENDER_OPTIONS = { lineWidth: 0, flowCollectionPadding: false } as const

/** Whether a rendered field is a form the BACKEND reader cannot decode.
 *
 *  `SKILL_LOADER` strips quote characters and resolves bare `|` / `>` block scalars,
 *  and does nothing else. Two rendered shapes are therefore off-limits, both
 *  measured against the real reader rather than inferred: a quoted scalar carrying a
 *  backslash escape (the quotes come off, the escape does not, so the value gains
 *  backslashes) and an explicit indentation indicator like `|2-` (the reader takes
 *  the header itself as the value and discards the content).
 *
 *  This is the writer's contract: it is bound by the READER's dialect, not by YAML. */
function readerCannotDecode(rendered: string): boolean {
  const header = rendered.split('\n')[0] ?? ''
  return /:\s[|>][+-]?\d/.test(header) || /:\s(".*\\|'.*\\)/.test(header)
}

/** Whether the VALUE's own text begins or ends with a quote character.
 *
 *  The reader unquotes with `value.strip("\"'")`, which cannot tell a wrapping quote
 *  from one that belongs to the text: `description: Runs "build"` reads back as
 *  `Runs "build`, and `it's a mess'` loses its final apostrophe. Measured, along with
 *  the rescue -- a bare block literal carries either faithfully.
 *
 *  This asks about the VALUE, not the rendered line, and the distinction is the whole
 *  point: a correctly wrapper-quoted scalar starts and ends with a quote by
 *  construction, and the reader strips exactly those two and is right to. Testing the
 *  rendered form instead sent `"  padded"` -- quoted only to protect its leading
 *  spaces -- down the block-literal path, where no bare form can keep that whitespace,
 *  so it came back as `padded`. Interior quotes are never at risk either way. */
function valueHasBoundaryQuote(value: string): boolean {
  return /^["']/.test(value) || /["']$/.test(value)
}

/** Serialize one managed key through the YAML writer, so a value needing quotes,
 *  a block scalar or escaping gets them. `lineWidth: 0` disables folding —
 *  without it a long description the author typed on one line comes back
 *  reflowed across several. Returns null when the field is absent, which means
 *  the key is not written at all.
 *
 *  `name` is written even when empty: it is the skill's identity, and the create
 *  form is gated on it being non-empty anyway. */
function renderManaged(key: string, data: SkillFormData): string | null {
  const doc = new Document({})
  let boundaryQuoted = false
  if (key === 'always') {
    if (!data.always) return null
    doc.set('always', true)
  } else if (key === 'tags') {
    if (!data.tags) return null
    const items = data.tags.split(',').map(t => representable(t).trim()).filter(Boolean)
    if (!items.length) return null
    const seq = doc.createNode(items) as YAMLSeq
    // Flow, to match how the editor has always written this field.
    seq.flow = true
    doc.set('tags', seq)
  } else {
    const value = representable(managedText(key, data))
    if (!value && key !== 'name') return null
    doc.set(key, value)
    boundaryQuoted = valueHasBoundaryQuote(value)
  }
  const rendered = doc.toString(RENDER_OPTIONS).replace(/\n$/, '')
  if (key === 'always' || key === 'tags') return rendered
  if (!boundaryQuoted && !readerCannotDecode(rendered)) return rendered

  /* The rendered form is one the READER cannot decode. A BARE block literal is
     decodable and needs no escaping, so try that first -- it fixes the quoted-escape
     case with no change to the value. */
  const value = representable(managedText(key, data))
  const asBlock = asBlockLiteral(key, value)
  if (asBlock !== null && !readerCannotDecode(asBlock)) return asBlock

  /* Still undecodable, which means YAML wanted an explicit indentation indicator: the
     value's first CONTENT line begins with whitespace, and no bare block form can keep
     that. Drop the leading blank lines and that indentation -- the same bounded loss
     the previous line-based assembler had, and far better than losing the value.
     Both steps matter: stripping only line 0 left `\n  indented` (blank first line,
     indented second) still needing `|2-`, and the fall-through below then SHIPPED
     `description: |2-`, which makes the backend take the header as the whole value and
     discard the text entirely. */
  const lines = value.split('\n')
  while (lines.length > 1 && lines[0].trim() === '') lines.shift()
  lines[0] = lines[0].replace(/^\s+/, '')
  const normalized = lines.join('\n')
  const retried = asBlockLiteral(key, normalized)
  if (retried !== null && !readerCannotDecode(retried)) return retried

  /* Last resort, and it is CHECKED rather than assumed: this function's contract is
     that it never emits a form the reader cannot decode, so the final exit is not
     allowed to be the one place that ignores it. If even the plain rendering is
     undecodable, collapse the value to a single line -- newlines are what force the
     forms the reader cannot read, and a flattened value still says what it said. */
  const plain = new Document({})
  plain.set(key, normalized)
  const plainRendered = plain.toString(RENDER_OPTIONS).replace(/\n$/, '')
  if (!readerCannotDecode(plainRendered)) return plainRendered

  const flat = new Document({})
  flat.set(key, normalized.replace(/\s*\n\s*/g, ' ').trim())
  return flat.toString(RENDER_OPTIONS).replace(/\n$/, '')
}

/** Rewrite only the managed keys inside the original block.
 *
 *  The parser decides what is a key, what continues a value and what is a
 *  comment — by the YAML grammar, not by matching line shapes. `#1790` spent
 *  four review rounds proving that enumeration cannot be completed (indented
 *  lines → block scalars → indented keys → blank lines → indentless lists), and
 *  the residual case it left open (`#1825`) was a top-level line the line-based
 *  walk could only attach to the preceding key, so re-emitting a managed key
 *  destroyed it. Source ranges have no such gap: a comment, a quoted
 *  `"my.key"` or a dotted key simply is not inside any managed key's range, so it
 *  is copied through where it stands.
 *
 *  Untouched bytes are COPIED, not re-serialized. `Document.toString()` would
 *  normalize the document — an indentless list becomes indented, a `>` scalar is
 *  re-folded — and both are byte changes to a field the form does not own, which
 *  the recorded invariant forbids. A managed field whose value the user did not
 *  change is copied too, so its original style, quoting and spacing survive. */
function spliceManaged(block: string, doc: Document.Parsed, data: SkillFormData): string {
  const original = metaOf(doc, block)
  const items = isMap(doc.contents) ? (doc.contents.items as Pair<unknown, unknown>[]) : []
  const written = new Set<string>()
  let out = ''
  let cursor = 0
  /* Where the LAST field's value ends, before the newline normalization below.
     The gap before the closing fence is by definition the bytes after that point;
     measuring it as trailing whitespace instead captured newlines that BELONG to a
     keep-chomped (`|+`) scalar, whose trailing blank lines are content. */
  let lastFieldEnd = 0

  for (const pair of items) {
    const key = pairKey(pair)
    const keyRange = isScalar(pair.key) ? pair.key.range : null
    if (!key || !keyRange) continue
    const valueRange = pair.value ? (pair.value as Node).range : null
    const start = keyRange[0]
    let end = valueRange ? valueRange[1] : keyRange[1]
    if (end > lastFieldEnd) lastFieldEnd = end
    // A BLOCK value's range ends PAST its terminating newline (measured: a `|`
    // or `>` scalar and a block sequence all report an end pointing at the next
    // key's first character), while a plain scalar's and a flow collection's
    // stops before it. Normalize to the latter shape so the newline always lands
    // in the copied-through region rather than inside a field's range. Without
    // this, replacing a multiline managed value glues the following key onto the
    // new value, and dropping one takes the following line with it.
    if (end > start && block[end - 1] === '\n') end--
    if (end > start && block[end - 1] === '\r') end--
    // Ranges arrive in source order; a duplicate or overlapping one would emit
    // the same bytes twice, so refuse it rather than corrupt the block.
    if (start < cursor || end < start) continue

    // Everything between the previous field and this key: comments, blank lines,
    // orphan lines. Not ours, so it is copied exactly.
    out += block.slice(cursor, start)
    cursor = end

    if (!MANAGED_KEYS.has(key) || written.has(key)) {
      out += block.slice(start, end)
      continue
    }
    written.add(key)

    // An UNTOUCHED field is copied first, before the drop branch below can see
    // it. Order matters: a field whose value is legitimately empty in the file
    // (`tags: []`, a bare `triggers:`, `always: false`) renders as "absent", so
    // asking `renderManaged` first would delete a line the user never edited.
    if (managedUnchanged(key, data, original)) {
      out += block.slice(start, end)
      continue
    }

    const rendered = renderManaged(key, data)
    if (rendered === null) {
      // The field is gone. Take the rest of its line with it, or removing
      // `always: true` would leave a blank line where the key used to be. A
      // comment cannot be on this line -- `isRewritable` refuses such a block --
      // and a comment on the line ABOVE is untouched, since only this line goes.
      const newline = block.indexOf('\n', cursor)
      cursor = newline === -1 ? block.length : newline + 1
      continue
    }
    out += rendered
    // A trailing comment lives OUTSIDE the replaced range, so it is copied
    // through. A bare key's null value ends flush against that comment
    // (`triggers: # none yet` replaces `triggers: `, space included), which would
    // paste `#` straight onto the new value -- and YAML only starts a comment
    // after whitespace, so the marker would be read as part of the value and both
    // the value and the comment would be wrong. One space restores the boundary.
    if (block[cursor] === '#' && !/\s$/.test(out)) out += ' '
  }

  out += block.slice(cursor)

  // A managed field the file never declared is appended, in the form's order.
  const added = MANAGED_ORDER
    .filter(key => !written.has(key))
    .map(key => renderManaged(key, data))
    .filter((line): line is string => line !== null)
  if (!added.length) return out
  // Insert BEFORE the gap that precedes the closing fence rather than trimming it:
  // a blank line the author left there is theirs. The gap is the newline-led
  // whitespace AFTER the last field ends -- everything past that point is copied
  // verbatim, so the same run sits at the end of `out`.
  const gap = /(?:\r?\n[ \t]*)*$/.exec(block.slice(lastFieldEnd))?.[0] ?? ''
  const trailing = gap && out.endsWith(gap) ? gap : ''
  const head = out.slice(0, out.length - trailing.length)
  const joined = added.join('\n')
  return head ? `${head}\n${joined}${trailing}` : `${joined}${trailing}`
}

/** Assemble YAML frontmatter + body from structured fields */
export function assembleSkillContent(data: SkillFormData): string {
  // If raw mode was used, return raw content directly
  if (data.raw !== undefined) return data.raw

  const block = data.frontmatter
  const doc = block === undefined ? null : readBlock(block)
  const front = block !== undefined && isRewritable(block, doc)
    ? spliceManaged(block, doc as Document.Parsed, data)
    : MANAGED_ORDER
      .map(key => renderManaged(key, data))
      .filter((line): line is string => line !== null)
      .join('\n')

  // Joined rather than interpolated so the fence and the blank line separating
  // frontmatter from body stay structural literals, not translatable copy.
  return ['---', front, '---', '', data.body || `# ${data.name}\n`].join('\n')
}

/** The parser's own complaint about a frontmatter block, or null when it has
 *  none. Surfaced next to the raw editor so a rejected block explains itself.
 *  Deliberately the PARSER's text and not new product copy: this reports a
 *  malformed file, not a state of the app. */
export function frontmatterError(raw: string): string | null {
  const split = splitBlock(raw)
  if (!split || !split.block.trim()) return null
  // Parse with one leading newline so the reported line numbers match the
  // textarea, which shows the opening `---` as line 1 while `block` starts after
  // it. Without the shift the message points one line above the offending key.
  // This copy is for the MESSAGE only; the splice always parses `block` itself.
  const doc = parseDocument('\n' + split.block)
  /* First line only, and without its trailing colon. The parser appends a caret
     excerpt quoting the offending lines, which sit directly above in the textarea
     the user is looking at -- the duplication adds height without adding
     information, while the first line still carries the location ("at line 7,
     column 1"). That line ends in a colon introducing the excerpt, so dropping the
     excerpt has to drop the colon too, or the message dangles mid-sentence. */
  if (doc.errors.length === 0) return null
  return doc.errors[0].message.split('\n')[0].replace(/:\s*$/, '')
}

/** Whether the structured form can represent this content's frontmatter. False
 *  for a block the parser rejected, and for a root that is not a block mapping —
 *  in both cases the form would show empty inputs over content that a save then
 *  overwrites, so the editor stays raw instead. */
export function canEditStructured(raw: string): boolean {
  const split = splitBlock(raw)
  // An opener with no closer is refused: see `parseSkillContent`.
  if (!split) return !raw.startsWith('---')
  if (!split.block.trim()) return true
  return isRewritable(split.block, readBlock(split.block))
}

/** Parse raw skill content into structured form data.
 *
 *  A block the parser cannot fully accept comes back with `raw` set instead of
 *  `frontmatter`, which opens the raw editor. The structured form would have to
 *  guess where its fields live in bytes it could not parse, and a wrong guess
 *  rewrites the file. */
export function parseSkillContent(raw: string, key: string): SkillFormData {
  const slash = key.indexOf('/')
  const name = slash > 0 ? key.slice(slash + 1) : key
  const category = slash > 0 ? key.slice(0, slash) : ''

  const split = splitBlock(raw)
  if (!split) {
    /* An OPENER with no closer is not "a skill without frontmatter": the fields are
       there, they just have no terminator. Treating it as bodyless would let a
       structured save wrap the whole text -- fences and keys included -- inside a
       new frontmatter block, silently reclassifying every field as body. Edit it
       raw instead. */
    if (raw.startsWith('---')) {
      return { name, category, description: '', triggers: '', tags: '', always: false, body: '', raw }
    }
    return { name, category, description: '', triggers: '', tags: '', always: false, body: raw }
  }

  const doc = readBlock(split.block)
  const meta = doc ? metaOf(doc, split.block) : {}
  const parsed: SkillFormData = {
    // Fall back to the key-derived name only when the file does not DECLARE a
    // name. An explicit `name: ""` or `name:` is a value the user wrote, and
    // substituting the derived one here would make an untouched field read as
    // edited and overwrite it on the next save.
    name: 'name' in meta ? meta.name : name,
    category,
    description: meta.description || '',
    triggers: meta.triggers || '',
    tags: meta.tags || '',
    always: meta.always === 'true',
    body: split.body,
  }

  if (!split.block.trim()) return parsed
  return isRewritable(split.block, doc) ? { ...parsed, frontmatter: split.block } : { ...parsed, raw }
}

export default function SkillForm({ data, onChange, hideIdentity, allowRaw = true }: SkillFormProps) {
  /* A skill whose frontmatter YAML does not parse arrives with `raw` set, because
     the structured form cannot locate its fields in bytes the parser rejected.
     Open in raw mode so the user sees the real file instead of empty inputs over
     content a save would overwrite. */
  const [rawMode, setRawMode] = useState(data.raw !== undefined)

  const set = <K extends keyof SkillFormData>(key: K, value: SkillFormData[K]) =>
    onChange({ ...data, [key]: value })

  const switchToRaw = () => {
    const assembled = assembleSkillContent({ ...data, raw: undefined })
    onChange({ ...data, raw: assembled })
    setRawMode(true)
  }

  const switchToStructured = () => {
    if (data.raw !== undefined) {
      // Never hand the structured form a block it cannot represent: it would show
      // empty inputs, and the next save would write them over the real content.
      if (!canEditStructured(data.raw)) return
      const parsed = parseSkillContent(data.raw, data.category ? `${data.category}/${data.name}` : data.name)
      onChange({ ...parsed, raw: undefined })
    }
    setRawMode(false)
  }

  if (rawMode) {
    const rawValue = data.raw || ''
    const parseProblem = frontmatterError(rawValue)
    const structuredAvailable = canEditStructured(rawValue)
    const declineNamesComment = !structuredAvailable && commentIsTheOnlyObstacle(rawValue)
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-[12px] text-muted font-mono">{i18nT('components.skillForm.raw_yaml_markdown')}</span>
          {/* Offered only when the frontmatter can round-trip through the form. A
              block the splice declines has no structured view to switch to, so the
              affordance is absent -- and the note below says so, rather than leaving
              the user to wonder why a control they have used elsewhere is missing. */}
          {structuredAvailable && (
            <button className="text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors" onClick={switchToStructured}>{i18nT('components.skillForm.switch_to_structured_editor')}</button>
          )}
        </div>
        <textarea
          aria-label={i18nT('components.skillForm.raw_yaml_and_markdown')}
          className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring"
          rows={20}
          value={rawValue}
          onChange={e => onChange({ ...data, raw: e.target.value })}
        />
        {!structuredAvailable && (
          <p data-testid="skill-structured-unavailable" className="text-[12px] text-muted m-0">
            {i18nT(declineNamesComment
              ? 'components.skillForm.structured_editing_unavailable_comment'
              : 'components.skillForm.structured_editing_unavailable')}
          </p>
        )}
        {parseProblem && (
          /* `status`, not `alert`: the message recomputes on every keystroke while
             the YAML is invalid, and an assertive region would interrupt a screen
             reader mid-edit on each one. */
          <pre role="status" data-testid="skill-frontmatter-error" className="text-[12px] text-danger font-mono whitespace-pre-wrap m-0">{parseProblem}</pre>
        )}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {allowRaw && (
        <div className="flex justify-end">
          <button className="text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors" onClick={switchToRaw}>{i18nT('components.skillForm.edit_raw_markdown')}</button>
        </div>
      )}
      {!hideIdentity && <>
        <div>
          {/* label-has-for can't resolve the control through the custom <Input>
              component; the runtime association via htmlFor + id + aria-label is correct. */}
          {/* eslint-disable-next-line jsx-a11y/label-has-for */}
          <label htmlFor="skill-name" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.name')}</label>
          <Input id="skill-name" aria-label={i18nT('components.skillForm.name')} placeholder={i18nT('components.skillForm.e_g_my_tool')} value={data.name} onChange={e => set('name', e.target.value)} className="w-full" />
        </div>
        <div>
          {/* eslint-disable-next-line jsx-a11y/label-has-for -- control resolved at runtime via htmlFor + id */}
          <label htmlFor="skill-category" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.category')} <span className="text-muted font-normal">{i18nT('components.skillForm.optional')}</span></label>
          <Input id="skill-category" aria-label={i18nT('components.skillForm.category')} placeholder={i18nT('components.skillForm.e_g_utils_code')} value={data.category} onChange={e => set('category', e.target.value)} className="w-full" />
          <div className="text-[11px] text-muted mt-1">{i18nT('components.skillForm.groups_the_skill_in_the_list_leave_empty_for_the')}</div>
        </div>
      </>}
      <div>
        <label htmlFor="skill-description" className="text-[13px] font-semibold text-text mb-1 block">
          <span className="block mb-1">{i18nT('components.skillForm.description')}</span>
          <textarea id="skill-description" aria-label={i18nT('components.skillForm.description')} className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={3} placeholder={i18nT('components.skillForm.what_this_skill_does_and_when_the_agent_should_u')} value={data.description} onChange={e => set('description', e.target.value)} />
        </label>
      </div>
      <div>
        {/* label-has-for can't resolve the control through the custom <Input>
            component; the runtime association via htmlFor + id + aria-label is correct. */}
        {/* eslint-disable-next-line jsx-a11y/label-has-for */}
        <label htmlFor="skill-triggers" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.triggers')}</label>
        <Input id="skill-triggers" aria-label={i18nT('components.skillForm.triggers')} placeholder={i18nT('components.skillForm.keyword1_keyword2_keyword3')} value={data.triggers} onChange={e => set('triggers', e.target.value)} className="w-full" />
        <div className="text-[11px] text-muted mt-1">{i18nT('components.skillForm.comma_separated_keywords_that_activate_this_skil')}</div>
      </div>
      <div>
        {/* eslint-disable-next-line jsx-a11y/label-has-for -- control resolved at runtime via htmlFor + id */}
        <label htmlFor="skill-tags" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.tags')} <span className="text-muted font-normal">{i18nT('components.skillForm.optional')}</span></label>
        <Input id="skill-tags" aria-label={i18nT('components.skillForm.tags')} placeholder={i18nT('components.skillForm.skill_tool_aws')} value={data.tags} onChange={e => set('tags', e.target.value)} className="w-full" />
        <div className="text-[11px] text-muted mt-1">{i18nT('components.skillForm.comma_separated_labels_for_categorization_metada')}</div>
      </div>
      <div className="flex items-center gap-2">
        <label htmlFor="skill-always" className="flex items-center gap-2 text-[13px] text-text cursor-pointer">
          <input type="checkbox" id="skill-always" aria-label={i18nT('components.skillForm.always_loaded')} checked={data.always} onChange={e => set('always', e.target.checked)} className="accent-accent" />
          <span>{i18nT('components.skillForm.always_loaded')} <span className="text-muted">{i18nT('components.skillForm.inject_full_content_into_every_session')}</span></span>
        </label>
      </div>
      <div>
        <label htmlFor="skill-instructions" className="text-[13px] font-semibold text-text mb-1 block">
          <span className="block mb-1">{i18nT('components.skillForm.instructions')}</span>
          <textarea id="skill-instructions" aria-label={i18nT('components.skillForm.instructions')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring" rows={10} placeholder={i18nT('components.skillForm.my_skill_step_by_step_instructions_for_the_agent')} value={data.body} onChange={e => set('body', e.target.value)} />
        </label>
      </div>
    </div>
  )
}
