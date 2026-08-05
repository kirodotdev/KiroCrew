import tsParser from '@typescript-eslint/parser'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import reactHooksPlugin from 'eslint-plugin-react-hooks'
import jsxA11y from 'eslint-plugin-jsx-a11y'

export default [
  {
    ignores: ['src/vite-env.d.ts'],
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 2020,
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      '@typescript-eslint': tsPlugin,
      'react-hooks': reactHooksPlugin,
      'jsx-a11y': jsxA11y,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...Object.fromEntries(Object.entries(jsxA11y.configs.recommended.rules || {}).map(([k, v]) => [k, 'warn'])),
      'jsx-a11y/no-autofocus': 'off',
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // The `highlight.js` barrel registers all ~190 bundled grammars (~200-240 KB
      // gzip). `src/utils/hljs.ts` wraps `highlight.js/lib/core` with only the
      // grammars the dashboard actually renders, so every main-thread caller must
      // go through it. Type-only imports are exempt: they erase at compile time and
      // carry no runtime weight (`utils/hljsLanguages.ts` needs `HLJSApi`).
      '@typescript-eslint/no-restricted-imports': ['error', {
        paths: [{
          name: 'highlight.js',
          message: "Import the core build instead: `import hljs from '<relative>/utils/hljs'`. The full barrel pulls every bundled grammar into the eager bundle.",
          allowTypeImports: true,
        }],
      }],
      'no-console': 'warn',
    },
  },
]
