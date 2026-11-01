/**
 * Single source of truth for the per-part Level-2 theme experience.
 *
 * An installed Level-2 pack has up to three parts, each on or off on its own:
 * sounds, overlays (overlays plus the topbar strip) and persona. Its colors
 * always apply while it is selected. Two values are stored per slug:
 *
 *  - the CHOICE, `mc-theme-experience-<slug>` = JSON `{sounds, overlays, persona}`.
 *    Its presence means the user has answered. A part the record does not list
 *    is off, so a part a later re-install adds never turns on by itself.
 *  - the persona GRANT, `mc-theme-consent-<slug>`: the sha256 of the persona text
 *    the user saw. api/client/chat.ts transmits it raw as `theme_consent_sha`,
 *    and the backend injects the persona only when it equals sha256 of the
 *    persona.md it reads. The grant exists only while the persona part is on;
 *    sounds and overlays never read or write it, so a pack without a persona
 *    never holds a value chat would send.
 *
 * Every write goes through saveExperienceChoice, which writes or revokes the
 * grant from the persona flag, so the two values cannot disagree. When
 * localStorage refuses the choice it is kept in memory for this tab, and when it
 * refuses the grant the persona part is saved as off.
 *
 * Read model for the grant:
 *  - hasConsent(slug, expectedToken) — STRICT: the stored grant equals the
 *    current persona fingerprint, so a changed persona asks again.
 *  - hasAnyConsent(slug) — RELAXED: some non-legacy grant is stored. Legacy `'1'`
 *    from an older build is NOT a grant.
 */
import { safeGetItem, safeRemoveItem, safeSetItem } from './safeStorage'

/** localStorage key prefix for the per-slug persona grant. */
export const CONSENT_PREFIX = 'mc-theme-consent-'

/** localStorage key prefix for the per-slug part choice. */
export const EXPERIENCE_PREFIX = 'mc-theme-experience-'

/**
 * Grant written by older builds. Never a valid grant: personas are bound to a
 * sha256, so any `'1'` asks again.
 */
const LEGACY_TOKEN = '1'

/**
 * Persona token for a pack whose descriptor carries no `personaInfo` (a
 * pre-upgrade backend). The backend never matches it, so no persona is injected;
 * older builds also wrote it as the grant for every consented pack.
 */
const SENTINEL_TOKEN = 'consented-v2'

/**
 * Window event fired after the choice or the grant of a slug changes, with
 * `{ slug }` as detail. The experience layer and the Settings section live in
 * different trees, and the browser's `storage` event does not fire in the tab
 * that wrote the value, so both re-read on this event.
 */
export const THEME_EXPERIENCE_CHANGED_EVENT = 'mc-theme-experience-changed'

export type ExperiencePart = 'sounds' | 'overlays' | 'persona'
export const EXPERIENCE_PARTS: readonly ExperiencePart[] = ['sounds', 'overlays', 'persona']
export type ExperienceChoice = Partial<Record<ExperiencePart, boolean>>

/** What a pack ships, as the consent model sees it. */
export interface ExperiencePack {
  sounds: boolean
  overlays: boolean
  persona: boolean
  /** The value a persona grant is bound to; null when the pack ships no persona. */
  personaToken: string | null
  /** Persona text shown before the grant is written, when the backend sent it. */
  personaText?: string
}

/** The loose descriptor shape experiencePack reads (a subset of ThemeAssets). */
interface PackAssets {
  overlays?: unknown
  topbar?: { dark?: boolean; light?: boolean }
  hasAudio?: boolean
  hasPersona?: boolean
  personaInfo?: { sha256: string; text?: string }
}

/** The parts a Level-2 descriptor ships. */
export function experiencePack(assets: PackAssets | undefined): ExperiencePack {
  const persona = !!assets?.hasPersona
  return {
    sounds: !!assets?.hasAudio,
    overlays:
      (Array.isArray(assets?.overlays) && assets.overlays.length > 0) ||
      !!(assets?.topbar?.dark || assets?.topbar?.light),
    persona,
    personaToken: persona ? assets?.personaInfo?.sha256 ?? SENTINEL_TOKEN : null,
    personaText: persona ? assets?.personaInfo?.text : undefined,
  }
}

/** The parts `pack` ships, in display order. */
export function shippedParts(pack: ExperiencePack): ExperiencePart[] {
  return EXPERIENCE_PARTS.filter((p) => pack[p])
}

function notifyExperienceChanged(slug: string): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(THEME_EXPERIENCE_CHANGED_EVENT, { detail: { slug } }))
}

/** Remove any stored persona grant for `slug` (best-effort; never throws). */
export function revokeConsent(slug: string): void {
  if (!slug) return
  safeRemoveItem(CONSENT_PREFIX + slug)
}

/** Raw stored grant for `slug`, or null if none / storage unavailable. */
export function getStoredConsent(slug: string): string | null {
  if (!slug) return null
  return safeGetItem(CONSENT_PREFIX + slug)
}

/** STRICT check: the stored grant for `slug` equals `expectedToken`. */
export function hasConsent(slug: string, expectedToken: string): boolean {
  const stored = getStoredConsent(slug)
  return stored !== null && stored === expectedToken
}

/** RELAXED check: some non-legacy, non-empty grant is stored for `slug`. */
export function hasAnyConsent(slug: string): boolean {
  const stored = getStoredConsent(slug)
  return stored !== null && stored !== '' && stored !== LEGACY_TOKEN
}

/**
 * Choices saveExperienceChoice could not write to localStorage, kept for this
 * tab. An entry is newer than whatever storage holds for that slug.
 */
const fallbackChoices = new Map<string, ExperienceChoice>()

/**
 * The choice for `slug`, or null when the user has not answered. A choice kept
 * for this tab wins over the stored one.
 */
export function getExperienceChoice(slug: string): ExperienceChoice | null {
  if (!slug) return null
  const kept = fallbackChoices.get(slug)
  if (kept) return { ...kept }
  const raw = safeGetItem(EXPERIENCE_PREFIX + slug)
  if (raw === null) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const out: ExperienceChoice = {}
  for (const part of EXPERIENCE_PARTS) {
    const v = (parsed as Record<string, unknown>)[part]
    if (typeof v === 'boolean') out[part] = v
  }
  return out
}

/**
 * Store the choice for `slug`. The persona grant is written, bound to
 * `personaToken`, only when `choice.persona` is true and a token is given;
 * otherwise any grant is revoked. Persona on with no token is stored as wanted
 * and unbacked, which readExperience asks about. A grant that cannot be written
 * turns the persona off, and a choice that cannot be written is kept for this
 * tab only. Returns true when the choice and any grant asked for were stored.
 */
export function saveExperienceChoice(
  slug: string,
  choice: ExperienceChoice,
  personaToken: string | null,
): boolean {
  if (!slug) return false
  const record: ExperienceChoice = {}
  for (const part of EXPERIENCE_PARTS) record[part] = choice[part] === true
  const wantsGrant = record.persona === true && !!personaToken
  // The choice is stored before any grant: a grant left without a stored
  // choice reads as an older build's all-on consent after a reload.
  if (!safeSetItem(EXPERIENCE_PREFIX + slug, JSON.stringify(record))) {
    if (wantsGrant) record.persona = false
    revokeConsent(slug)
    fallbackChoices.set(slug, record)
    notifyExperienceChanged(slug)
    return false
  }
  fallbackChoices.delete(slug)
  let persisted = true
  if (!wantsGrant) {
    revokeConsent(slug)
  } else if (!safeSetItem(CONSENT_PREFIX + slug, personaToken as string)) {
    record.persona = false
    revokeConsent(slug)
    if (!safeSetItem(EXPERIENCE_PREFIX + slug, JSON.stringify(record))) fallbackChoices.set(slug, record)
    persisted = false
  }
  notifyExperienceChanged(slug)
  return persisted
}

/** "Keep colors only": every part off, any grant revoked. */
export function keepColorsOnly(slug: string): void {
  saveExperienceChoice(slug, { sounds: false, overlays: false, persona: false }, null)
}

/**
 * Bring the stored state of `slug` in line with what `pack` ships. Called by the
 * experience layer once the server-confirmed Level-2 descriptor is known.
 *
 *  - No choice, an older build's grant that matches `pack`: every shipped part on.
 *  - Any grant the choice does not back (persona off or not shipped, or a hash
 *    that no longer matches the persona) is revoked, so it is never sent.
 */
export function settleExperience(slug: string, pack: ExperiencePack): void {
  if (!slug) return
  let choice = getExperienceChoice(slug)
  const grant = getStoredConsent(slug)
  if (!choice) {
    const legacyToken = pack.personaToken ?? SENTINEL_TOKEN
    if (grant !== null && grant === legacyToken) {
      const all: ExperienceChoice = {}
      for (const part of shippedParts(pack)) all[part] = true
      saveExperienceChoice(slug, all, pack.personaToken)
      return
    }
    choice = {}
  }
  const backed = pack.persona && choice.persona === true && grant !== null && grant === pack.personaToken
  if (grant !== null && !backed) {
    revokeConsent(slug)
    notifyExperienceChanged(slug)
  }
}

/** Which parts are on for `slug` right now, and what still needs an answer. */
export interface ExperienceState {
  /** True once a choice is stored. */
  answered: boolean
  sounds: boolean
  overlays: boolean
  persona: boolean
  /** Parts the consent dialog asks about; empty when no dialog is due. */
  ask: ExperiencePart[]
}

/**
 * Resolve the live state from storage. Pure: never writes.
 *
 * Without a choice, a pack that ships sounds or a persona asks about every part
 * it ships, and nothing runs until it is answered; a pack that ships only
 * overlays shows them. With a choice, a part is on only when the pack ships it
 * and the choice lists it as on; the persona additionally needs a grant equal to
 * the current persona fingerprint, and asks again (persona only) when it does not.
 */
export function readExperience(slug: string, pack: ExperiencePack): ExperienceState {
  const choice = getExperienceChoice(slug)
  if (!choice) {
    // An older build's grant reads as settleExperience will migrate it, so the
    // first commit before that runs neither flashes the dialog nor mounts anything.
    const grant = getStoredConsent(slug)
    if (grant !== null && grant === (pack.personaToken ?? SENTINEL_TOKEN)) {
      return { answered: true, sounds: pack.sounds, overlays: pack.overlays, persona: pack.persona, ask: [] }
    }
    const ask = pack.sounds || pack.persona ? shippedParts(pack) : []
    return { answered: false, sounds: false, overlays: ask.length === 0 && pack.overlays, persona: false, ask }
  }
  const personaWanted = pack.persona && choice.persona === true
  const persona = personaWanted && pack.personaToken !== null && getStoredConsent(slug) === pack.personaToken
  return {
    answered: true,
    sounds: pack.sounds && choice.sounds === true,
    overlays: pack.overlays && choice.overlays === true,
    persona,
    ask: personaWanted && !persona ? ['persona'] : [],
  }
}

/** Subscribe to changes of the stored state of `slug`, in this tab or another. */
export function subscribeExperience(slug: string, onChange: () => void): () => void {
  if (typeof window === 'undefined' || !slug) return () => {}
  const onChanged = (e: Event) => {
    if ((e as CustomEvent<{ slug?: string }>).detail?.slug === slug) onChange()
  }
  const onStorage = (e: StorageEvent) => {
    if (e.key === EXPERIENCE_PREFIX + slug || e.key === CONSENT_PREFIX + slug) {
      // Another tab stored a newer answer: this tab's kept choice is stale, and
      // chat sends whatever grant storage holds, so read storage again.
      fallbackChoices.delete(slug)
      onChange()
    }
  }
  window.addEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChanged)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChanged)
    window.removeEventListener('storage', onStorage)
  }
}
