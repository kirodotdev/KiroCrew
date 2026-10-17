/// <reference types="vite/client" />

declare const __APP_VERSION__: string
/** The find_ui auto tier's build digest, defined by the ui-auto-stamp Vite plugin; absent in tests. */
declare const __UI_AUTO_BUILD_DIGEST__: string | undefined

// Resolved by the `editionExtensionPlugin` in vite.config.ts: an inert empty
// module in the stock build, or the downstream edition's composition root when
// KIROCREW_EDITION_DIR is set. Imported once by src/extensions.ts.
declare module 'virtual:kirocrew-edition' {}

// Resolved by the `editionLanguagesPlugin` in vite.config.ts: an empty list in
// the stock build, or the edition's `languages.ts` when KIROCREW_EDITION_DIR is
// set. Imported by src/utils/highlightLanguages.ts (main thread and workers).
declare module 'virtual:kirocrew-edition-languages' {
  // Untrusted edition data; validateHighlightLanguages checks its shape.
  const languages: unknown
  export default languages
}
