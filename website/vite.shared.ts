/**
 * Pieces shared by every Vite config in this package — the app build
 * (`vite.config.ts`) and the story bundle (`.storybook/vite.config.ts`). One
 * spelling, imported by both, so a change lands in every bundle at once.
 */

/**
 * Force a SINGLE instance of every CONTEXT-CARRYING singleton across a bundle.
 * A KIROCREW_EDITION_DIR in a separate repo may resolve these from ITS OWN
 * node_modules; a second copy binds an edition component's hooks to a DIFFERENT
 * context instance than the core's providers — "Invalid hook call" (react),
 * "No QueryClient set" / null router context / silently empty data (the rest) —
 * only at runtime, only in the out-of-repo edition build. Dedupe the libraries
 * the core's provider tree owns; harmless in the stock single-node_modules
 * build. (See website/AGENTS.md — edition peer-dep rule.)
 */
export const CONTEXT_SINGLETON_DEDUPE = [
  'react',
  'react-dom',
  'react-redux',
  'react-router',
  'react-router-dom',
  '@tanstack/react-query',
  'framer-motion',
]
