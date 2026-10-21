/**
 * Parser for the engine's `specs/outline.md`.
 *
 * Ported from spec-driven-presentation-maker's Web UI
 * (`web-ui/src/components/deck/outlineParser.ts`, MIT-0), which owns the format:
 * the engine's orchestrator workflow states the outline is "parsed by the web UI,
 * so the format is fixed". Keeping the same grammar here means a deck reads the
 * same in both surfaces, and a format change upstream is a diff against one file.
 *
 *   # Deck title                 (optional, first prose line)
 *   ## Chapter
 *   - [slug] One-sentence claim   -> slides/<slug>.json
 *     - body: what the slide says and how
 *     - visual: the form and elements to show
 *     - evidence: where the facts are
 *
 * Nothing is silently dropped: any other non-blank line is kept as prose.
 */

export type OutlineSubItemKey = 'body' | 'visual' | 'evidence'

export interface OutlineSlide {
  type: 'slide'
  slug: string
  message: string
  body: string
  visual: string
  evidence: string
}

export interface OutlineSection {
  type: 'section'
  title: string
}

export interface OutlineProse {
  type: 'prose'
  text: string
}

export type OutlineEntry = OutlineSlide | OutlineSection | OutlineProse

/** A chapter and the slides under it. `section` is null for slides before the first `##`. */
export interface OutlineChapter {
  section: OutlineSection | null
  slides: OutlineSlide[]
  prose: OutlineProse[]
}

export interface ParsedOutline {
  /** The deck title from a leading `# Title` line, or null. */
  title: string | null
  chapters: OutlineChapter[]
  slideCount: number
}

const SLIDE_RE = /^-\s*\[([^\]]+)\]\s*(.*)/
const SUB_ITEM_RE = /^\s+-\s*(body|visual|evidence):\s*(.*)/
const SECTION_RE = /^##\s+(.+)/
// `\s+` then a non-space anchor and no trailing `\s*$`: linear in the line
// length, where a lazy group followed by `\s*$` backtracks quadratically on a
// crafted `# a␠␠…␠x` line and would freeze the page.
const TITLE_RE = /^#\s+(\S.*)/

export function parseOutlineEntries(markdown: string): OutlineEntry[] {
  const entries: OutlineEntry[] = []
  let current: OutlineSlide | null = null
  for (const line of markdown.split('\n')) {
    const slide = line.match(SLIDE_RE)
    if (slide) {
      current = { type: 'slide', slug: slide[1], message: slide[2].trim(), body: '', visual: '', evidence: '' }
      entries.push(current)
      continue
    }
    const sub = line.match(SUB_ITEM_RE)
    if (sub && current) {
      current[sub[1] as OutlineSubItemKey] = sub[2].trim()
      continue
    }
    const section = line.match(SECTION_RE)
    if (section) {
      current = null
      entries.push({ type: 'section', title: section[1].trim() })
      continue
    }
    if (line.trim() === '') continue
    current = null
    entries.push({ type: 'prose', text: line })
  }
  return entries
}

/** Group entries into chapters and lift a leading `# Title` out as the deck title. */
export function parseOutline(markdown: string): ParsedOutline {
  const entries = parseOutlineEntries(markdown)
  let title: string | null = null
  const first = entries[0]
  if (first?.type === 'prose') {
    const match = first.text.match(TITLE_RE)
    if (match) {
      title = match[1].trimEnd()
      entries.shift()
    }
  }
  const chapters: OutlineChapter[] = []
  let chapter: OutlineChapter = { section: null, slides: [], prose: [] }
  for (const entry of entries) {
    if (entry.type === 'section') {
      if (chapter.section || chapter.slides.length || chapter.prose.length) chapters.push(chapter)
      chapter = { section: entry, slides: [], prose: [] }
    } else if (entry.type === 'slide') {
      chapter.slides.push(entry)
    } else {
      chapter.prose.push(entry)
    }
  }
  if (chapter.section || chapter.slides.length || chapter.prose.length) chapters.push(chapter)
  return {
    title,
    chapters,
    slideCount: chapters.reduce((sum, c) => sum + c.slides.length, 0),
  }
}

/** The engine marks missing information with `[TBD]`; the view calls it out. */
export function hasTbd(text: string): boolean {
  return text.includes('[TBD]')
}
