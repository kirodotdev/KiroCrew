/**
 * What a `ui.find` match IS, decided from the matched element's identity and
 * where it sits, never from the words the agent asked with.
 *
 * Two questions, both answered in the tab:
 *
 * - `isTrustRootTarget`: the element belongs to the agent's own ceiling, so
 *   the guide neither points at it, nor resolves it, nor reports it as found.
 *   That is the case on a trust-root page (`TRUST_ROOT_ROUTES`, whatever the
 *   guide's route said), inside a region that marks itself
 *   `data-guide-trust-root` (the Security, Computer Use and Secrets panels
 *   wherever they render, a chat approval prompt and its trust menu, an app
 *   or project trust dialog, the approval-mode menu, and whatever any of them
 *   renders through a portal: `trustRoot.ts`), on a registered location of those tabs,
 *   on a settings row `isSensitiveSetting` refuses, or on a control whose
 *   catalog key or English name widens what the agent may do (approve,
 *   grant, trust, allow).
 * - `isCautionTarget`: the element removes something, so the panel shows the
 *   caution line and a press alone never ends the guide. That holds for a
 *   control marked `data-guide-caution` (or inside one), for a registered
 *   location the index marks `caution` (`GUIDE_CAUTION_LOCATIONS`), and for
 *   any control whose catalog key names a removal: the key of its registered
 *   location or auto site, or a key the active catalog renders as its name.
 *   Keys are the same in every language, so a Chinese "删除" is as cautious
 *   as an English "Delete".
 *
 * A name rendered from a catalog value with `{{...}}` parts maps back to that
 * value's key when the name carries all of the value's static text in order,
 * from its start to its end (`catalogKeysForName`), so a Chinese
 * "信任“npm test”" resolves to the key of "信任“{{cmd}}”".
 */
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import type { SettingEntry } from '../components/commandPalette/settingsTypes'
import { i18next } from '../i18n/index'
import { GUIDE_CAUTION_LOCATIONS, GUIDE_PLANS } from '../uiLocations/guidePlans.gen'
import { closestRegistered, registeredId } from '../uiLocations/targetRegistry'
import { GUIDE_CAUTION_ATTR, GUIDE_TRUST_ROOT_ATTR } from './trustRoot'

/** Pages of the agent's own ceiling (`TRUST_ROOT_ROUTES` in guide_catalog.py). */
export const TRUST_ROOT_ROUTES = ['/settings/security', '/settings/computer-use', '/settings/secrets'] as const

export function isTrustRootPath(pathname: string): boolean {
  const p = pathname.replace(/\/+$/, '').toLowerCase()
  return TRUST_ROOT_ROUTES.some(r => p === r || p.startsWith(`${r}/`))
}

/** Settings tabs of the agent's own ceiling: `settings.show` and `ui.find` refuse them. */
export const SENSITIVE_TABS: ReadonlySet<string> = new Set(['security', 'secrets', 'computer-use'])
/** Settings rows outside those tabs that widen what the agent may do. */
export const SENSITIVE_IDS: ReadonlySet<string> = new Set([
  'developer.chat-on-a-crew',
  'skills.require-approval-before-generated-skills-go-live',
])
const CREDENTIAL_RE = /token|secret|password|api-key|credential|client-id/i

/** Settings the guide never points at, even though the registry lists them: a
 *  credential field or a control that widens what the agent is allowed to do.
 *  A guide proposed by the agent must not walk the human to its own ceiling. */
export function isSensitiveSetting(entry: SettingEntry): boolean {
  if (SENSITIVE_TABS.has(entry.tab) || SENSITIVE_IDS.has(entry.id)) return true
  // Only an input can hold a credential; a toggle named "show context tokens"
  // holds none.
  return entry.type === 'input' && (CREDENTIAL_RE.test(entry.id) || CREDENTIAL_RE.test(entry.label))
}

const TRUST_ROOT_LOCATION_RE = /^(?:settings\.tab\.(?:security|computer-use|secrets)$|settings\.sub\.(?:security|computer-use|secrets)\.|setting:(?:security|computer-use|secrets)\.)/
/** Auto-site parents of the ceiling (`TRUST_ROOT_PARENTS` in scripts/lib/ui-index.mjs). */
const TRUST_ROOT_AUTO_PARENTS: ReadonlySet<string> = new Set(['settings.tab.security', 'settings.tab.computer-use', 'settings.tab.secrets'])

/** Words of a catalog key's last segment: `clearAllHistory` / `clear_all` -> `clear all ...`. */
export function keyLeafWords(key: string): string[] {
  const leaf = key.split('.').pop() ?? ''
  return leaf.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)
}

const REMOVAL_WORDS: ReadonlySet<string> = new Set(['delete', 'remove', 'uninstall', 'erase', 'reset', 'wipe', 'purge', 'destroy'])
const CLEARED: ReadonlySet<string> = new Set(['all', 'data', 'everything', 'history', 'cache', 'memory'])
const CEILING_WORDS: ReadonlySet<string> = new Set(['approve', 'grant', 'trust', 'allow', 'autopilot', 'yolo'])

/** Whether a catalog key names a removal (the generator's `CAUTION_LABEL_RE`, over key words). */
export function isRemovalKey(key: string): boolean {
  const w = keyLeafWords(key)
  return w.some((x, i) => REMOVAL_WORDS.has(x) || (x === 'clear' && CLEARED.has(w[i + 1] ?? '')))
}

/** Whether a catalog key names a control that widens what the agent may do. */
export function isCeilingKey(key: string): boolean {
  return keyLeafWords(key).some(x => CEILING_WORDS.has(x))
}

/** The English ceiling rule (`CEILING_LABEL_RE` in guide_catalog.py), for a name shown in English. */
const CEILING_NAME_RE = /\b(?:approve|grant|trust|allow|autopilot|yolo)\b/i

const collapse = (s: string) => s.replace(/\s+/g, ' ').trim()

/** A catalog value built at run time (`Trust <mono>{{cmd}}</mono>`): its static text, in order, spacing removed. */
interface Template { parts: string[]; key: string }

let reverse: { store: unknown; lng: string; map: Map<string, string[]>; templates: Template[] } | null = null

const noSpace = (s: string) => s.replace(/\s+/g, '')
/** Markup a `<Trans>` value carries around its parts (`<mono>`, `<1/>`): not part of the shown name. */
const TAG_RE = /<\/?[A-Za-z0-9]+\s*\/?>/g
/** Less static text than this matches too many names to say anything about one. */
const MIN_TEMPLATE_STATIC = 2

/** *s* without its `<Trans>` markup, removed until none is left (a tag can hide inside another). */
function stripTags(s: string): string {
  let prev: string
  do {
    prev = s
    s = s.replace(TAG_RE, '')
  } while (s !== prev)
  return s
}

function flatten(node: unknown, prefix: string, out: Map<string, string[]>, templates: Template[]): void {
  if (!node || typeof node !== 'object') return
  for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') {
      if (v.includes('{{')) {
        const parts = stripTags(v).split(/\{\{[^}]*\}\}/).map(noSpace)
        if (parts.join('').length >= MIN_TEMPLATE_STATIC) templates.push({ parts, key })
        continue
      }
      const name = collapse(v)
      if (!name) continue
      const keys = out.get(name)
      if (keys) keys.push(key); else out.set(name, [key])
    } else flatten(v, key, out, templates)
  }
}

/** Whether *name* (spacing removed) is what *t* renders for some values: its static parts, in order, anchored at both ends. */
function templateMatches(t: Template, name: string): boolean {
  const first = t.parts[0]
  const last = t.parts[t.parts.length - 1]
  if (!name.startsWith(first) || !name.endsWith(last) || name.length < first.length + last.length) return false
  let at = first.length
  const end = name.length - last.length
  for (const part of t.parts.slice(1, -1)) {
    const i = name.indexOf(part, at)
    if (i < 0 || i + part.length > end) return false
    at = i + part.length
  }
  return true
}

/**
 * The catalog keys whose value, in the language on screen (or English, which
 * stands in for a missing translation), is *name*: exactly, or, for a value
 * with `{{...}}` parts, with *name* carrying all its static text in order
 * from start to end (`信任“npm test”` is `信任“<mono>{{cmd}}</mono>”`).
 */
export function catalogKeysForName(name: string): readonly string[] {
  const want = collapse(name)
  if (!want || !i18next.isInitialized) return []
  const lng = i18next.language
  if (!reverse || reverse.store !== i18next.store || reverse.lng !== lng) {
    const map = new Map<string, string[]>()
    const templates: Template[] = []
    for (const code of new Set([lng, 'en'])) flatten(i18next.getResourceBundle(code, 'translation'), '', map, templates)
    reverse = { store: i18next.store, lng, map, templates }
  }
  const bare = noSpace(want)
  const fromTemplates = reverse.templates.filter(t => templateMatches(t, bare)).map(t => t.key)
  const exact = reverse.map.get(want) ?? []
  return fromTemplates.length ? [...exact, ...fromTemplates] : exact
}

/** Every location id *el* answers to: its registered location (or the one around it) and its auto site. */
function identityIds(el: Element): { location: string | null; auto: string | null } {
  return {
    location: closestRegistered(el, 'location')?.id ?? null,
    auto: registeredId(el, 'auto') ?? null,
  }
}

/** The label key an auto site id carries: `auto:<parent>:<file stem>:<key>[:<n>]`. */
function autoKey(siteId: string): string | null {
  const parts = siteId.split(':')
  return parts.length >= 4 && parts[0] === 'auto' ? parts[3] : null
}

function autoParent(siteId: string): string | null {
  const parts = siteId.split(':')
  return parts[0] === 'auto' && parts.length >= 2 ? parts[1] : null
}

/** The catalog keys that name *el*: its location's or auto site's, and any its shown name renders from. */
function keysOf(el: Element, name: string): string[] {
  const { location, auto } = identityIds(el)
  const keys: string[] = []
  const plan = location && Object.prototype.hasOwnProperty.call(GUIDE_PLANS, location) ? GUIDE_PLANS[location] : undefined
  if (plan) keys.push(plan.label_key)
  const ak = auto ? autoKey(auto) : null
  if (ak) keys.push(ak)
  keys.push(...catalogKeysForName(name))
  return keys
}

/** The settings registry row *el* is drawn in, when it is one. */
function settingEntryOf(el: Element): SettingEntry | undefined {
  const byId = el.closest('[data-setting-id]')?.getAttribute('data-setting-id')
  if (byId) return SETTINGS_REGISTRY.find(e => e.settingId === byId)
  const byKey = el.closest('[data-setting-key]')?.getAttribute('data-setting-key')
  if (byKey) return SETTINGS_REGISTRY.find(e => e.configKey === byKey)
  return undefined
}

/** Whether *el* is part of the agent's own ceiling (see the module comment). */
export function isTrustRootTarget(el: Element, name: string): boolean {
  if (isTrustRootPath(window.location.pathname)) return true
  if (el.closest(`[${GUIDE_TRUST_ROOT_ATTR}]`)) return true
  const { location, auto } = identityIds(el)
  if (location && TRUST_ROOT_LOCATION_RE.test(location)) return true
  const parent = auto ? autoParent(auto) : null
  if (parent && TRUST_ROOT_AUTO_PARENTS.has(parent)) return true
  const entry = settingEntryOf(el)
  if (entry && isSensitiveSetting(entry)) return true
  if (CEILING_NAME_RE.test(name)) return true
  return keysOf(el, name).some(isCeilingKey)
}

/** Whether a registered location id is part of the agent's own ceiling. */
export function isTrustRootLocationId(id: string): boolean {
  return TRUST_ROOT_LOCATION_RE.test(id)
}

/**
 * Whether *el* may be pointed at as the control that opens a `ui.find`
 * target's menu: never one of the agent's own ceiling, and never one that
 * removes something (an opener step carries no caution line, so a
 * destructive control is never passed off as "open this").
 */
export function isSafeOpenerTarget(el: Element, name: string): boolean {
  return !isTrustRootTarget(el, name) && !isCautionTarget(el, name)
}

/** Whether *el* removes something (see the module comment). */
export function isCautionTarget(el: Element, name: string): boolean {
  if (el.closest(`[${GUIDE_CAUTION_ATTR}]`)) return true
  const { location, auto } = identityIds(el)
  if (location && GUIDE_CAUTION_LOCATIONS.includes(location)) return true
  if (auto && GUIDE_CAUTION_LOCATIONS.includes(auto)) return true
  return keysOf(el, name).some(isRemovalKey)
}
