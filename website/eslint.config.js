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
      // A native <select> renders an OS-drawn popup: it ignores every theme
      // token, cannot be styled per row, and looks nothing like the rest of the
      // dashboard. Every dropdown goes through the shared Radix components —
      // SettingsSelect / SimpleSelect / SearchableSelect / DropdownMenu. See
      // website/docs/page-layout.md §Forms.
      //
      // 'error', not 'warn', on purpose: the tree is at zero, so this is a
      // hard-zero gate rather than a stored count, and it stays out of the
      // --max-warnings budget where a real regression would be indistinguishable
      // from an unrelated no-explicit-any.
      'no-restricted-syntax': ['error', {
        selector: "JSXOpeningElement[name.name='select']",
        message: 'No native <select> — its popup is drawn by the OS and ignores the theme. Use SimpleSelect, SearchableSelect, SettingsSelect, or DropdownMenu. See website/docs/page-layout.md.',
      }],
    },
  },
  {
    // The Mochi sub-windows (settings.html / avatar.html / panel.html) are
    // separate Electron entry points. Each ships its OWN inline <style> block
    // with hardcoded colors and the system font stack, and loads neither
    // Tailwind nor the theme tokens — so the shared token-based dropdowns would
    // render unstyled there. They keep their native selects until that renderer
    // is brought onto the dashboard's styling.
    files: ['src/apps/mochi/src/renderer/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
  {
    // Test doubles are exempt: a `vi.mock` that swaps a portalled Radix dropdown
    // for a plain <select> is the ESTABLISHED way to make one driveable in jsdom
    // (Radix commits discrete events through flushSync, which throws inside
    // Testing Library's act() — see src/test/CrewEditorSelect.test.tsx). Nothing
    // here renders to a user.
    files: ['src/**/*.test.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
]
