/**
 * Template-name helpers for a crewmate's bindings.
 *
 * A crewmate bound to a shared template is named by that template. A crewmate
 * that owns its own config file no longer derives a single "built from"
 * template — its abilities are the sum of its worker base plus whatever was
 * added to it (see the editor's Capabilities view) — so it is named generically
 * as a mate rather than by a provenance it no longer tracks.
 */
import { i18nT } from '../../i18n/t'

/** The built-in template a crewmate starts from unless another is picked. */
export const DEFAULT_TEMPLATE = 'kirocrew'

/** Source-defined agents a crewmate runs on that are not templates anyone picks. */
const SOURCE_DEFINED = new Set(['kirocrew-mate-conductor', 'kirocrew-mate-worker'])

/** A template's name as the user sees it: the default one is labelled as such. */
export function templateDisplayName(name: string): string {
  if (!name || name === DEFAULT_TEMPLATE || SOURCE_DEFINED.has(name)) {
    return i18nT('pages.membersPage.built_from_default', { agent: DEFAULT_TEMPLATE })
  }
  return name
}

/**
 * The label for a crewmate that owns its own config file: a generic "mate",
 * since its abilities are the sum of its base plus additions, not one template.
 */
export function mateAbilitiesLabel(): string {
  return i18nT('pages.membersPage.built_from_mate_label')
}
