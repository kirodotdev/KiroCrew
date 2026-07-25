/// <reference types="vite/client" />

declare const __APP_VERSION__: string

// Resolved by the `editionExtensionPlugin` in vite.config.ts: an inert empty
// module in the stock build, or the downstream edition's composition root when
// KIROCREW_EDITION_DIR is set. Imported once by src/extensions.ts.
declare module 'virtual:kirocrew-edition' {}
