/**
 * Turn a break-nudge key from the backend into the sentence the companion says.
 *
 * The backend picks a nudge and returns a KEY (`break.water.3`), never English, so
 * the phrasing can be translated here and read naturally in every language.
 *
 * WHY A SWITCH OF LITERAL CALLS, and not the tidier lookup table you would reach for
 * first — both tidier shapes are wrong for this codebase:
 *
 *   * `` i18nT(`apps.crewCompanion.${key}`) `` assembles the key at runtime, so it
 *     appears nowhere in the source: extraction and dead-key tooling cannot see it,
 *     and a missing entry shows the user the raw string instead of failing in CI.
 *   * A `const KEYS = { … } as const` map of key strings puts those strings in value
 *     position, where the i18n lint reads them as untranslated user-facing text.
 *
 * A literal passed directly to `i18nT` is the one shape that is both statically
 * visible to the key checker and exempt from the string lint — so every key below is
 * written out at its call site. Verbose, and verbose is the point: this list is the
 * contract with the backend, and a reader can see all of it at once.
 *
 * Keep in sync with `BREAK_NUDGES` in
 * `src/kiro_crew/apps/builtins/crew_companion/reminders.py` — four kinds, five
 * phrasings each. The variety is deliberate: one fixed sentence per kind becomes
 * wallpaper within a day, so the backend rotates and avoids the previous one.
 */
import { i18nT } from '../../i18n/t'

/**
 * @returns the translated nudge, or null when the key is not one we ship.
 *
 * Null rather than a fallback: a nudge nobody can read is worse than no nudge, so the
 * companion stays quiet instead of announcing "break.water.3" — and a null is
 * something a test can catch, which a silent fallback string is not.
 */
export function nudgeTextFor(backendKey: string): string | null {
  switch (backendKey) {
    case 'break.water.1': return i18nT('apps.crewCompanion.break.water.1')
    case 'break.water.2': return i18nT('apps.crewCompanion.break.water.2')
    case 'break.water.3': return i18nT('apps.crewCompanion.break.water.3')
    case 'break.water.4': return i18nT('apps.crewCompanion.break.water.4')
    case 'break.water.5': return i18nT('apps.crewCompanion.break.water.5')

    case 'break.stretch.1': return i18nT('apps.crewCompanion.break.stretch.1')
    case 'break.stretch.2': return i18nT('apps.crewCompanion.break.stretch.2')
    case 'break.stretch.3': return i18nT('apps.crewCompanion.break.stretch.3')
    case 'break.stretch.4': return i18nT('apps.crewCompanion.break.stretch.4')
    case 'break.stretch.5': return i18nT('apps.crewCompanion.break.stretch.5')

    case 'break.distance.1': return i18nT('apps.crewCompanion.break.distance.1')
    case 'break.distance.2': return i18nT('apps.crewCompanion.break.distance.2')
    case 'break.distance.3': return i18nT('apps.crewCompanion.break.distance.3')
    case 'break.distance.4': return i18nT('apps.crewCompanion.break.distance.4')
    case 'break.distance.5': return i18nT('apps.crewCompanion.break.distance.5')

    case 'break.breathe.1': return i18nT('apps.crewCompanion.break.breathe.1')
    case 'break.breathe.2': return i18nT('apps.crewCompanion.break.breathe.2')
    case 'break.breathe.3': return i18nT('apps.crewCompanion.break.breathe.3')
    case 'break.breathe.4': return i18nT('apps.crewCompanion.break.breathe.4')
    case 'break.breathe.5': return i18nT('apps.crewCompanion.break.breathe.5')

    default: return null
  }
}
