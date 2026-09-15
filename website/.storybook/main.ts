import type { StorybookConfig } from '@storybook/react-vite'

/**
 * Storybook renders the shared primitives in isolation, in a real browser, under
 * every theme the dashboard ships. It is a development surface: nothing under
 * `.storybook/` or `*.stories.tsx` reaches the production bundle, which is built
 * from `index.html` alone.
 *
 * The builder is pointed at its own small Vite config rather than the app's
 * `vite.config.ts`: that file carries build-only plugins (service-worker hash
 * stamping, vendor runtime staging, precompression, the edition overlay) that
 * assume the app entry and would either no-op or fail against a story bundle.
 * The story build needs only React, the `@` alias, and the singleton dedupe list.
 */
const config: StorybookConfig = {
  framework: '@storybook/react-vite',
  stories: ['../src/**/*.stories.@(ts|tsx)'],
  addons: ['@storybook/addon-themes', '@storybook/addon-a11y'],
  core: {
    builder: {
      name: '@storybook/builder-vite',
      options: { viteConfigPath: '.storybook/vite.config.ts' },
    },
    disableTelemetry: true,
  },
  staticDirs: ['../public'],
}

export default config
