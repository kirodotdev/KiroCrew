/**
 * `ui.find`: find a control on the page by its accessible NAME, and, when it
 * is not visible, look inside the registered containers that could hold it.
 *
 * Everything here runs in the person's own tab and reads only the DOM: the
 * agent names a label (in the language the dashboard shows), an optional role
 * and an optional container hint, and the page answers found / ambiguous /
 * none. Page text never leaves the tab: the gateway is told only that answer,
 * a count, the match's role and, when the match is a registered control, its
 * location id and label key (`findReport`). The names of the match, of the
 * containers opened on the way and of the alternatives are shown to the person
 * in the guide panel, never sent.
 *
 * Matching (`searchByName`), over the visible interactive elements outside the
 * guide's own layer: an exact name first, then the same name with case,
 * spacing and punctuation folded; a name that only contains the label is
 * never the control. A `location` (a `find_ref`'s registered id)
 * goes first: a visible control carrying it is the match whatever its name
 * reads, and a control carrying another registered id is never a name match
 * for it. Of several matches, a section title (a heading, or a control
 * drawn in one) gives way to a control that is not one, and for a tab or a
 * settings entry the one in the page's navigation wins. `role` narrows the candidates while that still
 * finds a match, and is dropped when it finds none; `container`
 * narrows several matches to those whose surroundings carry that name, or,
 * as `#N` (or `N`), picks the Nth of them in the order the panel numbers
 * them. What the match IS is then judged on the matched element itself
 * (`findTargetPolicy.ts`): a match of the agent's own ceiling, on whatever
 * page and by whatever route, is `sensitive`, never pointed at, resolved or
 * reported as found.
 *
 * Probing (`probeForName`) opens only containers a shared primitive
 * registered as side-effect free (`probeRegistry.ts`), through that
 * primitive's own state: no click, key or other event is ever dispatched.
 * Each one is opened, searched (and its own newly registered containers, one
 * level further), then restored by its paired restore, with scroll positions
 * put back. Focus is never moved: an opened primitive skips its autofocus
 * while the probe holds it, and restoring focuses nothing. It stops at the
 * first hit, after `maxContainers` containers, at depth 2, when its time
 * budget is spent, when its signal aborts (the step changed, the guide
 * ended), when `abortProbes` is called (the person pressed Cancel), or at
 * once if the address changed under it. A real press, key or wheel outside
 * the guide's panel while it runs stops it too, and then nothing is restored:
 * what the person did is theirs. It does not start at all while the person's
 * focus is in a field they can type into, or once `interrupted` says they
 * acted since the step began. A container no primitive registered is never
 * opened: the guide asks the person to open it.
 */
import { i18nT } from '../i18n/t'
import { GUIDE_PLANS } from '../uiLocations/guidePlans.gen'
import { closestRegistered, registeredId } from '../uiLocations/targetRegistry'
import { isDisabled, isDisplayed } from './liveRegistry'
import { isTrustRootPath, isTrustRootTarget } from './findTargetPolicy'
import { GUIDE_LAYER_SELECTOR, isPersonInput, PERSON_INPUTS, probeTargetById, probeTargets, type ProbeEntry, type ProbeKind } from './probeRegistry'

export { isTrustRootPath, TRUST_ROOT_ROUTES } from './findTargetPolicy'

/** The roles a search may narrow to (`FIND_ROLES` in guide_catalog.py). */
export const FIND_ROLES = ['button', 'link', 'menuitem', 'tab', 'switch', 'checkbox', 'textbox', 'option'] as const
export type FindRole = typeof FIND_ROLES[number]

export function isFindRole(v: unknown): v is FindRole {
  return typeof v === 'string' && (FIND_ROLES as readonly string[]).includes(v)
}

export interface FindQuery {
  label: string
  role?: FindRole
  container?: string
  /**
   * The registered control this search is for (a `find_ref`'s location id):
   * a visible control carrying it (`data-ui-location`, or `data-guide-target`
   * for a row a shared primitive draws) is the match whatever its name reads
   * now. Several of them are told apart as several name matches are.
   */
  location?: string
}

/** The guide's own layer: never a match, never probed, and input there never stops a probe. */
const GUIDE_LAYER = GUIDE_LAYER_SELECTOR

/** The element's role: its explicit `role`, else the one its tag implies. */
export function roleOf(el: Element): string {
  const explicit = el.getAttribute('role')?.trim().split(/\s+/)[0]
  if (explicit) return explicit
  const tag = el.tagName.toLowerCase()
  if (tag === 'button' || tag === 'summary') return 'button'
  if (tag === 'a') return el.hasAttribute('href') ? 'link' : ''
  if (tag === 'select') return 'combobox'
  if (tag === 'textarea') return 'textbox'
  if (tag === 'input') {
    const type = (el.getAttribute('type') ?? 'text').toLowerCase()
    if (type === 'checkbox') return 'checkbox'
    if (type === 'radio') return 'radio'
    if (type === 'button' || type === 'submit' || type === 'reset' || type === 'image') return 'button'
    if (type === 'search') return 'searchbox'
    return 'textbox'
  }
  return ''
}

/** Whether an element of role *have* is what a search for *want* means. */
export function roleMatches(want: FindRole | undefined, have: string): boolean {
  if (!want) return true
  if (want === have) return true
  if (want === 'switch' || want === 'checkbox') return have === 'switch' || have === 'checkbox' || have === 'menuitemcheckbox'
  if (want === 'menuitem') return have.startsWith('menuitem')
  if (want === 'textbox') return have === 'searchbox' || have === 'combobox'
  if (want === 'option') return have === 'radio' || have === 'menuitemradio'
  return false
}

const collapse = (s: string) => s.replace(/\s+/g, ' ').trim()

/** Text of *el* as a reader would say it: hidden-from-AT parts left out. */
function readableText(el: Element): string {
  let out = ''
  for (const node of Array.from(el.childNodes)) {
    if (node.nodeType === 3) out += node.textContent ?? ''
    else if (node instanceof Element && node.getAttribute('aria-hidden') !== 'true' && !node.hasAttribute('hidden')) {
      out += node instanceof HTMLImageElement ? (node.alt ?? '') : ` ${readableText(node)} `
    }
  }
  return out
}

/**
 * A practical accessible name: `aria-labelledby`, `aria-label`, a form
 * control's own label (or placeholder), the readable text, then `title`.
 */
export function accessibleName(el: Element): string {
  const by = el.getAttribute('aria-labelledby')
  if (by) {
    const text = by.split(/\s+/).map(id => el.ownerDocument.getElementById(id)).filter((x): x is HTMLElement => !!x).map(readableText).join(' ')
    if (collapse(text)) return collapse(text)
  }
  const aria = el.getAttribute('aria-label')
  if (aria && collapse(aria)) return collapse(aria)
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
    const labels = Array.from(el.labels ?? []).map(readableText).join(' ')
    if (collapse(labels)) return collapse(labels)
    const ph = el.getAttribute('placeholder')
    if (ph && collapse(ph)) return collapse(ph)
    if (el instanceof HTMLInputElement && ['button', 'submit', 'reset'].includes(el.type) && collapse(el.value)) return collapse(el.value)
  } else {
    const text = collapse(readableText(el))
    if (text) return text
  }
  return collapse(el.getAttribute('title') ?? '')
}

/** Case, spacing and punctuation folded away (NFKC first, so full-width forms match). */
export function normalizeName(s: string): string {
  return s.normalize('NFKC').toLocaleLowerCase().replace(/[\p{P}\p{S}\s]+/gu, '')
}

export type Shown = (el: HTMLElement) => boolean

/**
 * A box too small to see: the 1px visually-hidden pattern a file input or a
 * screen-reader-only control uses. It is in the accessibility tree, but there
 * is nothing on screen to point at.
 */
function tooSmallToSee(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect()
  return r.width < 2 || r.height < 2
}

/**
 * The elements a person can operate: these tags and roles, minus a link
 * without an `href` and a hidden input (see {@link operable}).
 */
const OPERABLE_ROLES = [
  '[role="button"],[role="link"],[role="tab"],[role="switch"],[role="option"]',
  '[role="menuitem"],[role="menuitemcheckbox"],[role="menuitemradio"]',
  '[role="checkbox"],[role="radio"],[role="textbox"],[role="searchbox"],[role="combobox"]',
].join(',')
const OPERABLE_TAGS = ['a', 'button', 'input', 'select', 'textarea', 'summary']
const OPERABLE_SELECTOR = [...OPERABLE_TAGS, OPERABLE_ROLES].join(',')

/** Whether *el* matches {@link OPERABLE_SELECTOR} and can actually be operated. */
function operable(el: Element): boolean {
  if (!el.matches(OPERABLE_SELECTOR)) return false
  if (el instanceof HTMLAnchorElement && !el.hasAttribute('href') && !el.matches(OPERABLE_ROLES)) return false
  if (el instanceof HTMLInputElement && el.type === 'hidden') return false
  return true
}

/** How many operable elements *root* holds. */
const operableCount = (root: Element) => Array.from(root.querySelectorAll(OPERABLE_SELECTOR)).filter(operable).length

/** Every element a person can operate, outside the guide's own layer, that *shown* accepts. */
function interactive(root: ParentNode, shown: Shown): HTMLElement[] {
  const all = root.querySelectorAll<HTMLElement>(OPERABLE_SELECTOR)
  return Array.from(all).filter(el => operable(el) && !el.closest(GUIDE_LAYER) && shown(el) && !tooSmallToSee(el))
}

/**
 * The registered control *el* is, when it is one: the id marked on itself,
 * or on a wrapper holding only it. A wrapper around several controls (a page,
 * a panel) names none of them.
 */
export function ownLocation(el: Element): string | null {
  const own = markOf(el)
  if (own) return own
  const wrap = markedWrapper(el)
  return wrap && operableCount(wrap) === 1 ? markOf(wrap) : null
}

/** The location *n* is registered as, curated or a shared primitive's row; never read off its attributes. */
function markOf(n: Element): string | null {
  return registeredId(n, 'location') ?? registeredId(n, 'target') ?? null
}

/** The nearest registered ancestor of *el* (itself excluded). */
function markedWrapper(el: Element): HTMLElement | null {
  for (let n = el.parentElement; n; n = n.parentElement) if (markOf(n) !== null) return n
  return null
}

/** Whether *el* is the registered control *id*: registered itself, or the one control inside a registered wrapper. */
function carriesLocation(el: Element, id: string): boolean {
  if (markOf(el) === id) return true
  const wrap = markedWrapper(el)
  return !!wrap && markOf(wrap) === id && operableCount(wrap) === 1
}

/** A heading, or a control drawn inside one (a section's title row), never an action's match while another candidate is not. */
const HEADING = 'h1, h2, h3, h4, h5, h6, [role="heading"]'
/** The page's navigation: a settings sub-nav entry, a tab, is looked for there first. */
const NAV_REGION = 'nav, [role="navigation"], [role="tablist"], [role="listbox"]'

/**
 * The chat transcript's own text: a link in a reply (Mate's own answer
 * naming "Artifacts") is something the person reads, never the control a
 * guide points at.
 */
const TRANSCRIPT_TEXT = '.message-bubble'

/** Whether *el* sits in the app's main navigation (the rail every page shares). */
export function inMainNav(el: Element): boolean {
  const main = i18nT('app.main_navigation')
  for (let n = el.closest('nav, [role="navigation"]'); n; n = n.parentElement?.closest('nav, [role="navigation"]') ?? null) {
    if (n.getAttribute('aria-label') === main) return true
  }
  return false
}

/** The name of what surrounds *el*: the nearest labelled ancestor, or a section's heading. */
export function contextOf(el: Element): string {
  let node = el.parentElement
  for (let depth = 0; node && depth < 16; depth += 1, node = node.parentElement) {
    if (node.closest(GUIDE_LAYER)) return ''
    const labelled = node.getAttribute('aria-label') || node.getAttribute('aria-labelledby')
    // A control around the match (a card that is itself a button, "View
    // details for X") names what pressing IT does, never where the match is.
    if (labelled && node.getAttribute('role') !== 'presentation' && !operable(node)) {
      const name = accessibleName(node)
      if (name) return name
    }
    const heading = node.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > header h1, :scope > header h2, :scope > header h3')
    if (heading) {
      const text = collapse(readableText(heading))
      if (text) return text
    }
  }
  return ''
}

export interface SearchResult {
  /** `sensitive`: the match is part of the agent's own ceiling; nothing points at it. */
  result: 'found' | 'ambiguous' | 'none' | 'sensitive'
  element: HTMLElement | null
  matches: HTMLElement[]
}

/** A `container` hint that is a number (`#2`, `2`), which is never a container's name: 1-based, or null. */
export function pickNumber(hint: string | undefined): number | null {
  const m = /^\s*#?\s*(\d{1,2})\s*$/.exec(hint ?? '')
  const n = m ? Number(m[1]) : 0
  return n >= 1 ? n : null
}

/** The visible controls named *q.label* (see the module comment for the order). */
export function searchByName(q: FindQuery, root: ParentNode = document, shown: Shown = isDisplayed): SearchResult {
  const want = collapse(q.label)
  const folded = normalizeName(want)
  if (!folded) return { result: 'none', element: null, matches: [] }
  const all = interactive(root, shown).map(el => ({ el, name: accessibleName(el), role: roleOf(el) }))
  const named = all.filter(c => !c.el.closest(TRANSCRIPT_TEXT))
  // The registered control itself, when the page draws it: its identity, not
  // its label, which can read differently in another state of the page.
  const own = q.location ? all.filter(c => carriesLocation(c.el, q.location!)) : []
  // A name match is the control only when its name IS the label (case,
  // spacing and punctuation folded): "Remove" is never "Report a problem —
  // secrets removed", "Add…" never "Add files & options". When the search is
  // for a registered control, another registered control is never it either,
  // whatever it is called.
  const byName = (pool: typeof all) => {
    const eligible = q.location ? pool.filter(c => { const l = ownLocation(c.el); return !l || l === q.location }) : pool
    const m = eligible.filter(c => c.name === want)
    return m.length > 0 ? m : eligible.filter(c => normalizeName(c.name) === folded)
  }
  // `role` is the kind of control the index knows the target as, and the page
  // does not always say it the same way: a settings sub-page's "tab" is a
  // plain button, a toggle is a button with `aria-pressed`. It narrows the
  // search when that finds something, and is dropped when it finds nothing.
  let matches = own.length > 0 ? own : byName(named.filter(c => roleMatches(q.role, c.role)))
  if (matches.length === 0 && q.role) matches = byName(named)
  // A control nested inside another match (a button inside a menu row) is
  // the same control said twice: keep the inner one.
  matches = matches.filter(m => !matches.some(o => o !== m && m.el.contains(o.el)))
  // A section's title (a heading, or a control drawn in one) is not the
  // action named like it, while a control that is not a title is there.
  if (matches.length > 1) {
    const notTitle = matches.filter(m => !m.el.closest(HEADING))
    if (notTitle.length > 0) matches = notTitle
  }
  // A tab or a sub-page entry is the one in the page's navigation, not a
  // control of the same name in the section it opens.
  if (matches.length > 1 && (q.role === 'tab' || q.location?.startsWith('settings.') || q.location?.startsWith('tab.'))) {
    const inNav = matches.filter(m => !!m.el.closest(NAV_REGION))
    if (inNav.length > 0) matches = inNav
    // The page's own sub-navigation, not the rail entry of the same name:
    // "Crewmates" the Customize tab, not "Crewmates" the main-menu page.
    const local = matches.filter(m => !inMainNav(m.el))
    if (local.length > 0 && local.length < matches.length) matches = local
  }
  // A numbered hint (`#2`) never narrows: a number names a match only in
  // the list the person saw, and the page may have reordered since. The
  // person picks in the panel, where the pick holds the control itself.
  if (matches.length > 1 && q.container && pickNumber(q.container) === null) {
    const hint = normalizeName(q.container)
    const near = hint ? matches.filter(m => normalizeName(contextOf(m.el)).includes(hint)) : []
    if (near.length > 0) matches = near
  }
  // Judged on what matched, never on what was asked; and a label that only
  // CONTAINS part of a ceiling control's name is refused too, though it
  // matches nothing: the person is never told to look for that control.
  if (matches.some(m => isTrustRootTarget(m.el, m.name))) return { result: 'sensitive', element: null, matches: [] }
  if (matches.length === 0 && all.some(c => normalizeName(c.name).includes(folded) && isTrustRootTarget(c.el, c.name))) {
    return { result: 'sensitive', element: null, matches: [] }
  }
  const els = matches.map(m => m.el)
  if (els.length === 0) return { result: 'none', element: null, matches: els }
  return els.length === 1 ? { result: 'found', element: els[0], matches: els } : { result: 'ambiguous', element: null, matches: els }
}

/**
 * The nearest section title above *el* in reading order: a heading before it
 * among its ancestors' earlier siblings (or inside them), nearest first.
 */
export function headingBefore(el: Element): string {
  for (let node: Element | null = el; node && node !== document.body; node = node.parentElement) {
    if (node.closest(GUIDE_LAYER)) return ''
    for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) {
      const heads = sib.matches(HEADING) ? [sib] : Array.from(sib.querySelectorAll(HEADING))
      const last = heads[heads.length - 1]
      const text = last ? collapse(readableText(last)) : ''
      if (text) return text
    }
  }
  return ''
}

/**
 * How the person can tell several matches apart: each one's surroundings
 * (`contextOf`), else the section title above it (`headingBefore`). Two that
 * read the same are named by the section title above each instead, never by
 * a bare number: the panel numbers its list itself. Shown in the panel only.
 */
export function distinguishingContexts(matches: readonly Element[]): string[] {
  // Matches drawn once per item of a list (Install on every app row) are told
  // apart by the item each is on, named as the row names it (its
  // `data-guide-pick`): "Install · Command Bar".
  const items = matches.map(m => closestRegistered(m, 'pick')?.id.trim() ?? '')
  if (matches.length > 1 && items.every(Boolean) && new Set(items).size === items.length) {
    return matches.map((m, i) => `${accessibleName(m)} · ${items[i]}`)
  }
  const near = matches.map(m => contextOf(m) || headingBefore(m))
  const named = near.map((c, i) => {
    if (c && near.filter(x => x === c).length === 1) return c
    return headingBefore(matches[i]) || c
  })
  // Matches of different kinds (the rail's Crewmates link and the Crewmates
  // tab) also say which kind each is, in the person's language: a region's
  // name alone ("Main navigation") does not say what to press there.
  const roles = matches.map(m => roleOf(m))
  if (new Set(roles).size < 2) return named
  // Each also leads with its own name, so "Crewmates · tab" and "Crewmates ·
  // link · Main navigation" read as two different controls, not two regions.
  return named.map((c, i) => {
    const own = accessibleName(matches[i])
    const parts = [own, roleWord(roles[i]), c && c !== own ? c : '']
    return parts.filter(Boolean).join(' · ')
  })
}

/** Catalog keys of the role words a candidate list names a control's kind by. */
export const ROLE_WORD_KEYS: Readonly<Record<string, string>> = {
  button: 'components.guideLayer.role_button',
  link: 'components.guideLayer.role_link',
  tab: 'components.guideLayer.role_tab',
  menuitem: 'components.guideLayer.role_menuitem',
  switch: 'components.guideLayer.role_switch',
  checkbox: 'components.guideLayer.role_checkbox',
  textbox: 'components.guideLayer.role_textbox',
  option: 'components.guideLayer.role_option',
  combobox: 'components.guideLayer.role_combobox',
}

function roleWord(role: string): string {
  const key = ROLE_WORD_KEYS[role]
  return key ? i18nT(key) : ''
}

// ── probing ──

export type ContainerKind = ProbeKind

/**
 * A container on the way to the control. `id` is its registration
 * (`probeRegistry.ts`); `name` and `role` re-find it when it has remounted.
 */
export interface ContainerRef {
  id: number
  name: string
  role: string
  kind: ContainerKind
}

export interface ProbeOptions {
  maxDepth?: number
  maxContainers?: number
  budgetMs?: number
  /** How long the page gets to render what an open showed. */
  settleMs?: number
  shown?: Shown
  now?: () => number
  /** Aborted when the step changes or the guide ends: the probe stops and restores. */
  signal?: AbortSignal
  /** Whether the person acted on the page since the step began: then the probe does not start, or stops. */
  interrupted?: () => boolean
}

export type ProbeResult =
  | { result: 'found'; path: ContainerRef[] }
  /** Several matches inside the container *path* reveals; the person picks one there. */
  | { result: 'ambiguous'; path: ContainerRef[]; count: number }
  | { result: 'none' }

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

/** Overlap of words between a container's name and what is searched for (0..1). */
function similarity(name: string, wants: readonly string[]): number {
  const words = (s: string) => new Set(s.normalize('NFKC').toLocaleLowerCase().split(/[\s\p{P}\p{S}]+/u).filter(Boolean))
  const have = words(name)
  if (have.size === 0) return 0
  let best = 0
  for (const w of wants) {
    const want = words(w)
    if (want.size === 0) continue
    let n = 0
    for (const x of want) if (have.has(x)) n += 1
    best = Math.max(best, n / want.size)
  }
  return best
}

interface Saved {
  scroll: Array<[Element | Window, number, number]>
}

function scrollParents(el: Element): Element[] {
  const out: Element[] = []
  for (let p = el.parentElement; p; p = p.parentElement) if (p.scrollHeight > p.clientHeight || p.scrollWidth > p.clientWidth) out.push(p)
  return out
}

function save(el: Element): Saved {
  return {
    scroll: [[window, window.scrollX, window.scrollY], ...scrollParents(el).map((p): [Element, number, number] => [p, p.scrollLeft, p.scrollTop])],
  }
}

function restoreView(s: Saved) {
  for (const [target, x, y] of s.scroll) {
    if (target === window) { if (window.scrollX !== x || window.scrollY !== y) window.scrollTo?.(x, y) } else {
      const e = target as Element
      if (e.scrollLeft !== x) e.scrollLeft = x
      if (e.scrollTop !== y) e.scrollTop = y
    }
  }
}

/** Whether the person's focus is in a field they type into: a probe then leaves the page alone. */
export function isEditingFocus(el: Element | null = document.activeElement): boolean {
  if (!(el instanceof HTMLElement) || el === document.body) return false
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) return true
  if (el instanceof HTMLInputElement) return !['button', 'submit', 'reset', 'checkbox', 'radio', 'image', 'range', 'color', 'file'].includes(el.type)
  return el.isContentEditable || el.closest('[contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]') !== null
}

/** A registered container's trigger, when the probe may open it now. */
function openable(e: ProbeEntry, shown: Shown): HTMLElement | null {
  const el = e.trigger()
  if (!el || !el.isConnected || e.isOpen()) return null
  if (!shown(el) || isDisabled(el) || el.closest(GUIDE_LAYER)) return null
  if (isTrustRootTarget(el, accessibleName(el))) return null
  return el
}

let probing = 0
/** The controllers of the probes running now (`abortProbes`). */
const running = new Set<AbortController>()

/**
 * Stop every running probe at once: each restores what it opened and
 * concludes nothing. For the person ending the guide, before the gateway
 * has answered.
 */
export function abortProbes(): void {
  for (const c of [...running]) c.abort()
}

/**
 * Whether a probe is holding a container open right now. What it opened is
 * not the person's doing, so nothing may count as found meanwhile.
 */
export function isProbing(): boolean {
  return probing > 0
}

/**
 * Look for *q* inside the page's registered containers (see the module
 * comment). Resolves with the path of containers that reveals it,
 * `ambiguous` (with that path) when the one that reveals it shows several,
 * or `none`. Leaves the page as it found it, unless the person acted
 * meanwhile.
 */
export async function probeForName(q: FindQuery, opts: ProbeOptions = {}): Promise<ProbeResult> {
  const own = new AbortController()
  const outer = opts.signal
  const forward = () => own.abort()
  if (outer?.aborted) own.abort(); else outer?.addEventListener('abort', forward, { once: true })
  running.add(own)
  probing += 1
  try {
    return await probeOnce(q, { ...opts, signal: own.signal })
  } finally {
    probing -= 1
    running.delete(own)
    outer?.removeEventListener('abort', forward)
  }
}

async function probeOnce(q: FindQuery, opts: ProbeOptions): Promise<ProbeResult> {
  const maxDepth = opts.maxDepth ?? 2
  const maxContainers = opts.maxContainers ?? 12
  const budgetMs = opts.budgetMs ?? 1500
  const settleMs = opts.settleMs ?? 60
  const shown = opts.shown ?? isDisplayed
  const now = opts.now ?? (() => performance.now())
  const signal = opts.signal
  const actedBefore = opts.interrupted ?? (() => false)
  if (signal?.aborted || actedBefore() || isEditingFocus() || isTrustRootPath(window.location.pathname)) return { result: 'none' }
  const deadline = now() + budgetMs
  const href = window.location.href
  let opened = 0
  let navigated = false
  // The person pressed, typed or scrolled somewhere outside the guide's
  // panel: stop, and leave what they now see alone. The probe itself
  // dispatches no events, so anything heard here is not its own doing.
  let interrupted = false
  const onInput = (e: Event) => { if (isPersonInput(e)) interrupted = true }
  for (const type of PERSON_INPUTS) window.addEventListener(type, onInput, true)
  const halted = () => !!signal?.aborted || interrupted || actedBefore() || navigated
  const wants = [q.container && pickNumber(q.container) === null ? q.container : '', q.label].filter(Boolean)

  const candidates = (exclude: ReadonlySet<number>): Array<{ entry: ProbeEntry; el: HTMLElement }> => {
    const out: Array<{ entry: ProbeEntry; el: HTMLElement; score: number; i: number }> = []
    probeTargets().forEach((entry, i) => {
      if (exclude.has(entry.id)) return
      const el = openable(entry, shown)
      if (el) out.push({ entry, el, score: similarity(accessibleName(el), wants), i })
    })
    return out.sort((a, b) => b.score - a.score || a.i - b.i)
  }

  const visit = async (depth: number, exclude: ReadonlySet<number>): Promise<ProbeResult> => {
    for (const { entry, el } of candidates(exclude)) {
      if (halted() || opened >= maxContainers || now() >= deadline) break
      if (probeTargetById(entry.id) !== entry || openable(entry, shown) !== el) continue
      const ref: ContainerRef = { id: entry.id, name: accessibleName(el), role: roleOf(el), kind: entry.kind }
      const before = new Set(probeTargets().map(e => e.id))
      const saved = save(el)
      opened += 1
      const restore = entry.open()
      // Waited out even when aborted: the restore reads the primitive's
      // state, so what the open did must have rendered first.
      await sleep(settleMs)
      if (window.location.href !== href) navigated = true
      let hit: ProbeResult = { result: 'none' }
      if (!halted() && entry.isOpen()) {
        const found = searchByName(q, document, shown)
        if (found.result === 'found') hit = { result: 'found', path: [ref] }
        else if (found.result === 'ambiguous') hit = { result: 'ambiguous', path: [ref], count: found.matches.length }
        else if (found.result === 'none' && depth < maxDepth) {
          const inner = await visit(depth + 1, before)
          if (inner.result !== 'none') hit = { ...inner, path: [ref, ...inner.path] }
        }
      }
      if (interrupted) return { result: 'none' }
      restore()
      await sleep(settleMs)
      if (interrupted) return { result: 'none' }
      restoreView(saved)
      if (halted()) break
      if (hit.result !== 'none') return hit
    }
    return { result: 'none' }
  }

  try {
    return await visit(1, new Set())
  } finally {
    for (const type of PERSON_INPUTS) window.removeEventListener(type, onInput, true)
  }
}

/**
 * The element a container step points at: the DEEPEST container of *path*
 * the page shows now (its parent is already open), by its registration, or
 * re-found by name and role among the registered triggers.
 */
export function resolveContainerPath(path: readonly ContainerRef[], shown: Shown = isDisplayed): HTMLElement | null {
  for (let i = path.length - 1; i >= 0; i -= 1) {
    const ref = path[i]
    const own = probeTargetById(ref.id)?.trigger()
    if (own && own.isConnected && shown(own)) return own
    const hit = probeTargets()
      .map(e => e.trigger())
      .filter((el): el is HTMLElement => !!el && el.isConnected && shown(el) && roleOf(el) === ref.role && accessibleName(el) === ref.name)
    if (hit.length === 1) return hit[0]
  }
  return null
}

// ── what the tab knows of each search ──

export type FindState =
  | { status: 'probing' }
  /** Not showing, and the registered control the index places it under (`location`) is: the person opens that. */
  | { status: 'opener'; location: string }
  /** `count`: the container holds several matches, which the person picks among once it is open. */
  | { status: 'found'; path: ContainerRef[]; count?: number }
  | { status: 'ambiguous'; count: number }
  | { status: 'none' }

const states = new Map<string, FindState>()

/**
 * One of several matches, as the panel numbered it: the element itself
 * (weakly: a remounted list drops it) and its signature, what the person
 * saw it as (name, role and surroundings).
 */
interface Candidate {
  el: WeakRef<HTMLElement>
  sig: string
  context: string
}

/**
 * The candidate the person picked, bound to THAT control, never to a number
 * or a position: `set` is every match's signature, in order, when they
 * picked. It points at the picked element while it is still connected and the
 * matches are still the same set. Once the set changes or the picked control
 * is gone (a remount draws a new element, which is never assumed to be the
 * same entity), the pick is stale and the person is asked again, even when
 * only one match remains.
 */
interface Pick {
  el: WeakRef<HTMLElement>
  sig: string
  index: number
  set: string[]
}

const picks = new Map<string, Pick>()
/** The numbered candidates last shown for each search, and every match's signature then. */
const shownCandidates = new Map<string, { list: Candidate[]; set: string[] }>()

/** What the person saw a match as: its name, role, surroundings, the section title above it and its registered identity. */
function signature(el: HTMLElement): string {
  const identity = closestRegistered(el, 'location')?.id ?? registeredId(el, 'auto') ?? ''
  return JSON.stringify([accessibleName(el), roleOf(el), contextOf(el), headingBefore(el), identity])
}

const sameSet = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((x, i) => x === b[i])

/** The picked control among *live*, or null when the pick is stale (see `Pick`). */
function pickedAmong(pick: Pick, live: readonly HTMLElement[]): HTMLElement | null {
  if (!sameSet(pick.set, live.map(signature))) return null
  const el = pick.el.deref()
  return el && el.isConnected && live.includes(el) && signature(el) === pick.sig ? el : null
}

/** One search's identity: what it looks for, in this guide action. */
export function findKey(guideId: string, actionIndex: number, q: FindQuery): string {
  return JSON.stringify([guideId, actionIndex, q.label, q.role ?? null, q.container ?? null, ...(q.location ? [q.location] : [])])
}

export function findState(key: string): FindState | undefined {
  return states.get(key)
}

export function setFindState(key: string, state: FindState | undefined): void {
  if (state) states.set(key, state); else states.delete(key)
}

/**
 * The person picked candidate *n* (1-based, as `findCandidates` last listed
 * them) of the search *key*. A number no listed candidate carries is no pick.
 */
export function setFindPick(key: string, n: number | null): void {
  const shown = shownCandidates.get(key)
  const c = n === null ? undefined : shown?.list[n - 1]
  if (!c || !shown || n === null) { picks.delete(key); return }
  picks.set(key, { el: c.el, sig: c.sig, index: n - 1, set: shown.set })
}

/** The number of the candidate the person picked (1-based), stale or not. */
export function findPick(key: string): number | undefined {
  const p = picks.get(key)
  return p ? p.index + 1 : undefined
}

/**
 * Whether the person still has to pick for *key*: several match and none is
 * picked, or a pick was made and has gone stale (see `Pick`) while some
 * control still carries the name.
 */
export function findNeedsPick(q: FindQuery, key: string, shown: Shown = isDisplayed): boolean {
  const live = searchByName(q, document, shown)
  if (live.result !== 'found' && live.result !== 'ambiguous') return false
  const pick = picks.get(key)
  if (!pick) return live.result === 'ambiguous'
  return pickedAmong(pick, live.matches) === null
}

/**
 * The numbered candidates of a search the person picks in, read from the
 * page while they are shown and kept (in this tab only) for when they are
 * not, such as after the menu holding them closed.
 */
export function findCandidates(q: FindQuery, key: string, shown: Shown = isDisplayed): string[] {
  const live = searchByName(q, document, shown)
  if (live.result === 'ambiguous' || (live.result === 'found' && findNeedsPick(q, key, shown))) {
    const contexts = distinguishingContexts(live.matches)
    shownCandidates.set(key, {
      list: live.matches.slice(0, 9).map((el, i) => ({ el: new WeakRef(el), sig: signature(el), context: contexts[i] })),
      set: live.matches.map(signature),
    })
  }
  return shownCandidates.get(key)?.list.map(c => c.context) ?? []
}

/** Every search forgotten (tests). */
export function resetFindStates(): void {
  states.clear()
  picks.clear()
  shownCandidates.clear()
}

/**
 * The control a `ui.find` search points at now: the one visible match when
 * nothing was picked, else the control the person picked while that pick
 * holds. Never a sensitive match.
 */
export function resolveFind(q: FindQuery, key: string, shown: Shown = isDisplayed): HTMLElement | null {
  const live = searchByName(q, document, shown)
  if (live.result !== 'found' && live.result !== 'ambiguous') return null
  const pick = picks.get(key)
  if (!pick) return live.result === 'found' ? live.element : null
  return pickedAmong(pick, live.matches)
}

/** A role the gateway knows (`FIND_ROLES`), for a found element. */
function reportRole(el: Element): FindRole | undefined {
  const have = roleOf(el)
  return FIND_ROLES.find(r => r === have) ?? (have.startsWith('menuitem') ? 'menuitem' : have === 'searchbox' || have === 'combobox' ? 'textbox' : undefined)
}

export interface FindReport {
  result: 'found' | 'ambiguous' | 'none'
  count: number
  role?: FindRole
  location_id?: string
  label_key?: string
}

/**
 * What the gateway may hear about this search right now: the live page first
 * (a visible match, a picked one, or several), else what the probe concluded.
 * Ids and counts only; never a name the page shows. A sensitive match is
 * reported as nothing at all.
 */
export function findReport(q: FindQuery, key: string, shown: Shown = isDisplayed): FindReport {
  const live = searchByName(q, document, shown)
  if (live.result === 'sensitive') return { result: 'none', count: 0 }
  const el = resolveFind(q, key, shown)
  if (el) {
    const lid = closestRegistered(el, 'location')?.id ?? registeredId(el, 'auto') ?? undefined
    const role = reportRole(el)
    const plan = lid && Object.prototype.hasOwnProperty.call(GUIDE_PLANS, lid) ? GUIDE_PLANS[lid] : undefined
    return {
      result: 'found',
      count: 1,
      ...(role ? { role } : {}),
      ...(lid && /^[A-Za-z0-9_.:-]{1,160}$/.test(lid) ? { location_id: lid } : {}),
      ...(plan && /^[a-z][A-Za-z0-9_.]{0,160}$/.test(plan.label_key) ? { label_key: plan.label_key } : {}),
    }
  }
  // Several, or a pick gone stale: the person picks (again).
  if (live.result === 'ambiguous' || live.result === 'found') return { result: 'ambiguous', count: Math.min(live.matches.length, 50) }
  const s = states.get(key)
  if (s?.status === 'ambiguous') return { result: 'ambiguous', count: Math.min(Math.max(s.count, 2), 50) }
  if (s?.status === 'found') {
    return s.count && s.count > 1
      ? { result: 'ambiguous', count: Math.min(s.count, 50) }
      : { result: 'found', count: 1, ...(q.role ? { role: q.role } : {}) }
  }
  return { result: 'none', count: 0 }
}
