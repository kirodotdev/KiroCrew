import tsParser from '@typescript-eslint/parser'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import reactHooksPlugin from 'eslint-plugin-react-hooks'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import approvalOneShotDecision from './eslint-rules/approval-one-shot-decision.js'
import { plugin as shadcn } from '@shadcn/lint'

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
      'approval-one-shot': approvalOneShotDecision,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      // The one-shot approval endpoint records no standing grant, so a trust
      // verb decided inline at the call site is laundered into `approve` before
      // the request is made — upstream of both the typed client and the
      // backend's 400. 'error', not 'warn': this is a consent path, and a
      // warning would ride the --max-warnings ratchet instead of failing.
      'approval-one-shot/no-inline-one-shot-decision': 'error',
      // Downgrade jsx-a11y's recommended severities to 'warn' so they ride the
      // --max-warnings ratchet instead of failing the build outright — but keep
      // whatever the preset switched OFF off. A blanket rewrite to 'warn' also
      // re-enables the rules the plugin deliberately disabled, which is how 44
      // `label-has-for` warnings existed: the plugin marks that rule
      // `deprecated: true, replacedBy: ['label-has-associated-control']` and
      // ships it as 'off' in recommended, while the live replacement is already
      // on. Those 44 were noise from a rule nobody chose, consuming ratchet
      // headroom that a real a11y regression needs.
      ...Object.fromEntries(
        Object.entries(jsxA11y.configs.recommended.rules || {}).map(([k, v]) => [
          k,
          v === 'off' || v === 0 ? v : 'warn',
        ]),
      ),
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
      // `no-eval` is not part of eslint:recommended, so without this line eval
      // is unlinted across the application tree. The tree is at zero eval sites
      // in .ts/.tsx (every `eval` match under src/ is prose in comments), so
      // like the native-<select> gate below this is a hard-zero 'error', not a
      // 'warn' riding the ratchet. The one deliberate eval lives in the .mjs
      // generator block below, guarded by its own reviewed directive.
      'no-eval': 'error',
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
    // `ui/native-select.tsx` is the ONE sanctioned native `<select>` in the
    // dashboard, and the exemption is deliberately the single file rather than a
    // directory: the rule's job is still to stop native selects being scattered,
    // and this file is the chokepoint that makes that enforceable — SimpleSelect
    // routes to it on coarse pointers, so no other module ever needs one.
    //
    // The rule's reason is theming, and that reason does not reach a phone. The
    // Radix popup's list is a `position:fixed` overflow scroller inside
    // react-remove-scroll's lock, and iOS Safari does not reliably hand a finger
    // drag to that shape: Settings → Voice → Language shows 7 of its ~41 BCP-47
    // codes and the rest cannot be reached at all. A themed list nobody can
    // scroll is worse than an OS-drawn list that works, so on touch the platform
    // draws it. Pointer devices are untouched and still get the themed popup.
    //
    // See website/docs/page-layout.md §Forms, which records the same exception.
    files: ['src/components/ui/native-select.tsx'],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
  {
    // `.mjs` build/codegen scripts under `src/` are matched by no other block, so
    // without this one they lint against an EMPTY rule set: the `no-eval` directive
    // in `crew-ghost-sprite.gen.mjs` sits above a real `eval()` and is reported as
    // unused, which is a warning that can never be burned down without deleting a
    // true statement. Enabling the rule the directive names makes it live, so the
    // exemption is a deliberate, reviewed one instead of an accident of config
    // coverage — and a second `eval()` here would now be an error.
    //
    // This block is ONE rule wide on purpose and that is a known gap: `.mjs` here
    // still gets no `no-unused-vars`, no `no-undef`, none of the base set the
    // `.{ts,tsx}` block above carries. It is scoped to the rule an existing
    // directive already named rather than guessing a rule set for a file type with
    // exactly one member (`crew-ghost-sprite.gen.mjs`). A SECOND `.mjs` file under
    // `src/` inherits that near-empty coverage silently, so widening this is the
    // right move the moment one lands — a codegen script wants different rules from
    // an application module, which is the decision being deferred, not skipped.
    files: ['src/**/*.mjs'],
    rules: {
      'no-eval': 'error',
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
  {
    // Design-system lint (@shadcn/lint). Reads the compiled Tailwind v4 theme
    // from src/index.css via components.json, so "known" means "this project's
    // Tailwind emits CSS for it" — custom @utility names and plain class
    // selectors in the theme's import graph count, a typo does not.
    //
    // Three rules are on, all hard-zero like the rest of this file:
    //   no-raw-colors           — palette colors (`text-green-500`) and literal
    //                             SVG colors instead of theme tokens. This is the
    //                             class-level half of scripts/check-theme-colors.mjs,
    //                             which only sees CSS/hex literals.
    //   no-unknown-classes      — a class Tailwind generates nothing for. Covers
    //                             every utility; scripts/check-phantom-classes.mjs
    //                             is scoped to color utilities.
    //   require-static-classes  — a className built from an opaque value on a
    //                             ui/ component; the other two rules cannot read it.
    //
    // no-restyle is NOT enabled: the tree carries ~300 call sites that restyle
    // ui/ components (DropdownMenuItem, TableCell, …) and CI runs --max-warnings 0,
    // so 'warn' would fail the build outright. Enabling it is a design decision
    // (fix the sites or write per-component contracts), not a lint toggle.
    // no-inline-styles and no-arbitrary-values are off by design: inline
    // `style={}` is the mandated styling method for apps (docs/app-kit), and the
    // theme's translucent surfaces are `bg-[color-mix(…)]` arbitrary values
    // because the color tokens carry no alpha channel.
    files: ['src/**/*.{ts,tsx}'],
    plugins: { shadcn },
    rules: {
      'shadcn/no-raw-colors': ['error', {
        // `fill-none` is `fill: none`, not a color; the rule reads any `fill-*`
        // it does not recognise as a palette name.
        allow: ['fill-none'],
      }],
      'shadcn/no-unknown-classes': ['error', {
        // "Unknown" means the theme's import graph produces no CSS for it. Two
        // kinds of class are real but invisible to that graph, and are allowed
        // here by the stylesheet that defines them. An entry allows a NAME, it
        // does not generate CSS: keep each one next to its defining file and
        // delete it when that file goes.
        allow: [
          // Apps whose stylesheet is a TS template string injected as <style>,
          // so no .css file exists for the linter to follow.
          'cc-*', 'is-remove',   // apps/crew-companion/styles.ts + panel.css
          'mc-fe-*',             // apps/file-explorer/styles.ts
          'mdnb-*',              // apps/md-notebook/styles.ts
          'sb-*',                // apps/spec-builder/inlineStyles.ts
          'wc-*',                // apps/issue-radar/WelcomeCarousel.tsx <style>
          'spin',                // components/PullRequestPanel.tsx <style>
          // Selector hooks with no CSS by design: Playwright specs, tests and
          // hooks/useMessageSearch.ts locate these elements by class.
          'message-bubble', 'input-area', 'chat-container', 'session-agent-label', 'primary',
          'pierre-editor-fallback',
        ],
      }],
      'shadcn/require-static-classes': 'error',
    },
  },
  {
    // Component implementations compose their own cva()/variant helpers at the
    // call site, which require-static-classes cannot resolve (the rule's own
    // docs prescribe this override). The other two rules stay on here.
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'shadcn/require-static-classes': 'off',
    },
  },
  {
    // The ghost mascot is artwork: its white and black are the drawing's own
    // colors, shared with the Electron boot sequence, not a theme surface.
    files: ['src/components/KiroGhost.tsx'],
    rules: {
      'shadcn/no-raw-colors': 'off',
    },
  },
  {
    // The Mochi renderer windows load neither Tailwind nor the theme tokens (see
    // the native-<select> override above), so "does Tailwind emit this class" and
    // "is this a theme color" have no meaning there: every class is a hook for
    // its own inline <style> block.
    files: ['src/apps/mochi/src/renderer/**/*.{ts,tsx}'],
    rules: {
      'shadcn/no-raw-colors': 'off',
      'shadcn/no-unknown-classes': 'off',
      'shadcn/require-static-classes': 'off',
    },
  },
  {
    // Tests pass throwaway class names (`custom-cls`, `seam-mark`) to assert that
    // a component forwards className; nothing here renders to a user.
    files: ['src/**/*.test.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    rules: {
      'shadcn/no-raw-colors': 'off',
      'shadcn/no-unknown-classes': 'off',
      'shadcn/require-static-classes': 'off',
    },
  },
]
