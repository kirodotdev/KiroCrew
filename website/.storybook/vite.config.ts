import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { CONTEXT_SINGLETON_DEDUPE } from '../vite.shared'

const here = path.dirname(fileURLToPath(import.meta.url))

/**
 * The Vite config the story bundle is built with. Deliberately NOT the app's
 * `vite.config.ts` (see `.storybook/main.ts` for why). It carries only the two
 * pieces a component needs to resolve at all — the `@` source alias and the
 * context-carrying singleton dedupe list, imported from the same module the app
 * build reads. Tailwind v4 needs the same Vite plugin as production to compile
 * `src/index.css` utilities for visual review.
 */
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    {
      name: 'storybook-edition-languages',
      enforce: 'pre',
      resolveId(id: string) {
        return id === 'virtual:kirocrew-edition-languages'
          ? '\0virtual:kirocrew-edition-languages'
          : null
      },
      load(id: string) {
        return id === '\0virtual:kirocrew-edition-languages'
          ? 'export default []\n'
          : null
      },
    },
  ],
  resolve: {
    alias: {
      '@': path.resolve(here, '../src'),
    },
    dedupe: CONTEXT_SINGLETON_DEDUPE,
  },
})
