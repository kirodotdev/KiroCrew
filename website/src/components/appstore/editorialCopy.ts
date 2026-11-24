/**
 * The curator's copy on a Discover editorial card, in the reader's language.
 *
 * The editorial document publishes one English string per field, so the
 * dashboard holds the translations: a catalog key for each title and blurb the
 * official document publishes, keyed by that exact English. Exact English, not
 * an id, so a reworded field misses the table and shows as published instead of
 * wearing a translation of words the curator no longer uses. A new or reworded
 * collection reads in English until a release adds its keys.
 *
 * Same lookup shape as `CATEGORY_LABEL_KEY` in `categories.ts`: a flat `Record`
 * of full literal keys indexed inline at the `i18nT()` call, so
 * `scripts/check-i18n-keys.mjs` can resolve it, and resolved during render so a
 * language switch re-resolves it. Published text is never handed to i18next.
 */

import { i18nT } from '../../i18n/t'

export const EDITORIAL_COPY_KEY: Record<string, string> = {
  'Ship better code': 'components.appstore.editorialCopy.ship_better_code_title',
  'Spec it, review it, measure it: the loop that keeps a codebase honest.':
    'components.appstore.editorialCopy.ship_better_code_blurb',
  'Research to deliverable': 'components.appstore.editorialCopy.research_to_deliverable_title',
  'From open questions to a paper and the deck that presents it.':
    'components.appstore.editorialCopy.research_to_deliverable_blurb',
}

// `hasOwnProperty`, not `in`: a published `toString` must not resolve to an
// inherited Object.prototype member.
const known = (text: string | undefined) =>
  text === undefined || Object.prototype.hasOwnProperty.call(EDITORIAL_COPY_KEY, text)

/**
 * A card's title and blurb, translated together or not at all: when either
 * field is one the table does not know, both render exactly as published, so a
 * reworded blurb never sits in English under a translated title.
 */
export function editorialCopy(copy: { title?: string; blurb?: string }): { title?: string; blurb?: string } {
  if (!known(copy.title) || !known(copy.blurb)) return copy
  return {
    title: copy.title === undefined ? undefined : i18nT(EDITORIAL_COPY_KEY[copy.title]),
    blurb: copy.blurb === undefined ? undefined : i18nT(EDITORIAL_COPY_KEY[copy.blurb]),
  }
}
