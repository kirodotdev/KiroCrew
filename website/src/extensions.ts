/**
 * Extension composition root — the ONE file a downstream edition owns.
 *
 * This module is imported for its side effects as the very first line of
 * `main.tsx`, before the store, providers, or `App` are constructed. It is the
 * single, documented place where a downstream edition (or plugin bundle) calls
 * the frontend extension-seam registrars so their contributions are present
 * before the shell renders:
 *
 *   import { registerBuiltinComponents } from './apps/builtinRegistry'
 *   import { registerBuiltinIcons }      from './apps/builtinIcons'
 *   import { registerThemeBranding }     from './themeBranding'
 *   import { registerTopBarWidgets }     from './apps/topBarWidgets'
 *   import { registerPanelShortcut }     from './hooks/useKeyboardShortcuts'
 *   import { registerNonAppPrefix }      from './components/MigrationCheck'
 *   import { registerTheme }             from './hooks/useTheme'
 *
 * For edition-owned API methods there is no registrar — the edition imports the
 * blessed `apiTransport` (`./api/apiTransport`) and builds its own typed API
 * module on it (the core never consumes edition API methods, so a registry would
 * add stringly-typed surface for no composition benefit).
 *
 * The core ships this file EMPTY — the stock build registers nothing and every
 * seam is inert. A downstream edition overlays / owns this one file to inject
 * its registrations, instead of forking `main.tsx` (the copy-and-shadow erosion
 * the seams exist to eliminate). The registries are read at module load /
 * first render, so registering here — before `main.tsx` mounts `App` — is the
 * supported contract; registering later is not reactive and may not appear
 * until an unrelated re-render.
 */
export {}
