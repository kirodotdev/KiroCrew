/**
 * Extension composition root — the core-owned entry point that pulls in a
 * downstream edition's registrations.
 *
 * This module is imported for its side effects as the very first line of
 * `main.tsx`, before the store, providers, or `App` are constructed, so any
 * edition contributions are present before the shell renders. The edition's OWN
 * `extensions.tsx` (in its own repo, at `$KIROCREW_EDITION_DIR`) is where the
 * frontend extension-seam registrars are called:
 *
 *   import { registerBuiltinComponents } from '@/apps/builtinRegistry'
 *   import { registerBuiltinIcons }      from '@/apps/builtinIcons'
 *   import { registerThemeBranding }     from '@/themeBranding'
 *   import { registerTopBarWidgets }     from '@/apps/topBarWidgets'
 *   import { registerPanelShortcut }     from '@/hooks/useKeyboardShortcuts'
 *   import { registerNonAppPrefix }      from '@/components/MigrationCheck'
 *   import { registerTheme }             from '@/hooks/useTheme'
 *   import { registerCapsuleSegment }    from '@/apps/capsuleSegments'
 *   import { registerOverviewStatCards } from '@/pages/overviewStatCards'
 *
 * For edition-owned API methods there is no registrar — the edition imports the
 * blessed `apiTransport` (`@/api/apiTransport`) and builds its own typed API
 * module on it (the core never consumes edition API methods, so a registry would
 * add stringly-typed surface for no composition benefit).
 *
 * The core OWNS this file and imports the `virtual:kirocrew-edition` module,
 * which the `editionExtensionPlugin` in `vite.config.ts` resolves to:
 *   - an INERT empty module in the stock OSS build (`KIROCREW_EDITION_DIR`
 *     unset) — the stock build registers nothing, byte-identical to no seam;
 *   - the downstream edition's OWN composition root
 *     (`$KIROCREW_EDITION_DIR/extensions.tsx`) when that env var points at an
 *     edition repo — so the edition injects its registrations by CONFIG, never
 *     by shadowing this file or `main.tsx` (the copy-and-shadow erosion the
 *     seams exist to eliminate).
 *
 * A downstream edition puts its `register*()` calls + component imports in its
 * own `extensions.tsx`; it does NOT edit this file. The registries are read at
 * module load / first render, so the edition module runs — before `main.tsx`
 * mounts `App` — via this import; registering later is not reactive.
 *
 * (Edition-owned API methods have no registrar — the edition imports the blessed
 * `apiTransport` and builds its own typed API module on it.)
 */
// Resolved by editionExtensionPlugin (vite.config.ts): inert in the stock build,
// the edition's composition root when KIROCREW_EDITION_DIR is set.
import 'virtual:kirocrew-edition'

export {}
