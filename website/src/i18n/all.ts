/**
 * The all-languages i18n entry.
 *
 * Importing it registers every catalog and re-exports the runtime API, so a
 * caller needs exactly one import. This is what production boots through and
 * what a test that switches language uses; `./index` is the English-only path
 * the vitest setup file takes. Why the two exist: `./index`'s header.
 */

import { CATALOGS } from './catalogs'
import { registerCatalogs } from './index'

// At module scope, so importing this entry is the whole registration step: a
// caller that forgot to call something would render bare keys.
registerCatalogs(CATALOGS)

export * from './index'
