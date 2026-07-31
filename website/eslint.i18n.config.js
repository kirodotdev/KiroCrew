/**
 * i18n lint, deliberately a SECOND eslint invocation rather than rules added to
 * `eslint.config.js`.
 *
 * `no-literal-string` at `mode: 'all'` reports on the order of a thousand findings
 * on this codebase. Folding those into the main config would push them into the
 * same `--max-warnings 1116` budget that guards `no-explicit-any`, `jsx-a11y` and
 * `no-console` — and an i18n regression would then be indistinguishable from a new
 * `any`. Separate config, separate budget, separate signal.
 *
 * ## Why `mode: 'all'` and not the default
 *
 * The plugin's default is `jsx-text-only`, which sees only plain text in JSX
 * markup. `jsx-only` adds JSX attributes. Neither sees a literal inside a JSX
 * *expression container* — `{cond ? 'Generating…' : 'Download Export (.zip)'}` —
 * and that is where the largest class of untranslated strings in this dashboard
 * lives, including the export and import buttons on the Portability tab.
 *
 * Template literals are a separate opt-in again: `should-validate-template` is
 * required on top of `all`, or `` `Show ${n} more app${n === 1 ? '' : 's'}` ``
 * stays invisible.
 *
 * The cost of `all` is false positives, so the noise is controlled by the
 * `include`/`exclude` regexes below rather than by weakening the mode — a narrower
 * mode does not report fewer false positives, it reports fewer findings of every
 * kind, including the ones that matter.
 */

import tsParser from '@typescript-eslint/parser'
import i18nextPlugin from 'eslint-plugin-i18next'

export default [
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: [
      'src/vite-env.d.ts',
      // Test files assert on visible English by design.
      'src/**/*.test.{ts,tsx}',
      'src/test/**',
      // Generated and data-only.
      'src/i18n/locales/**',
    ],
    linterOptions: {
      // Every `eslint-disable` comment in this codebase targets the MAIN config's
      // rules — `no-console`, `no-explicit-any`, `exhaustive-deps`. None of those are
      // enabled here, so each directive reports as a problem: 58 as unused-directive
      // warnings and a further 172 attributed to the disabled rule itself, at
      // severity ERROR, which fails the run regardless of `--max-warnings`.
      //
      // `reportUnusedDisableDirectives: 'off'` only silences the first group, and the
      // second cannot be silenced by declaring those rules `'off'` here without also
      // registering their plugins. The script therefore runs with
      // `--no-inline-config`, which is the correct semantics anyway: those comments
      // were written about a different config.
      //
      // The trade-off, stated: a developer cannot suppress an i18n finding with an
      // inline comment. For a ratchet that is arguably right — suppression goes
      // through the baseline number, so the debt stays visible as one figure instead
      // of scattering into comments nobody counts.
      reportUnusedDisableDirectives: 'off',
    },
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 2020,
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: { i18next: i18nextPlugin },
    rules: {
      'i18next/no-literal-string': [
        'warn',
        {
          mode: 'all',
          'should-validate-template': true,

          // Content-based exemptions, applied wherever the string appears.
          words: {
            exclude: [
              // Tailwind and CSS: class strings are the single largest false-positive
              // source under `mode: 'all'`.
              //
              // The bare character class was too permissive: it also matched ordinary
              // lowercase copy, so `'search failed'` and `'no results found'` were
              // exempted as if they were class strings and bypassed BOTH this gate and
              // the coverage ratchet. The negative lookahead carves prose back out.
              //
              // "Prose" here is deliberately narrow — two or more space-separated PLAIN
              // alphabetic words. That is the shape a class string never has: every
              // Tailwind utility carries a hyphen, digit, colon or bracket
              // (`items-center`, `gap-2`, `hover:bg-red-500`, `w-[3px]`), and API option
              // values are single tokens (`'short'`, `'numeric'`, `'2-digit'`, `'h23'`).
              // Requiring a CSS-specific character instead was tried and rejected: it
              // flagged ~3800 `Intl.DateTimeFormat` option literals, and a gate that
              // cries wolf 3800 times is a gate someone deletes.
              //
              // Known false negatives, stated rather than discovered later:
              //   1. SINGLE-word copy is still exempt — `'saved'`, `'active'`, `'done'`.
              //      Unavoidable by shape: it is indistinguishable from an API option
              //      value. This is precisely the class the `en-XA` pseudolocale in this
              //      PR catches by construction, which is why a render-time detector
              //      ships alongside the static ones rather than after them.
              //   2. prose containing a hyphen or digit — `'read-only mode'`, `'2 items'`.
              //   3. a bare-utility pair with no hyphen — `'flex hidden'` — is now
              //      flagged, so it lands in the baseline. Accepted: a false positive
              //      costs one baseline entry, a false negative hides copy forever.
              '^(?![a-z]+(?: [a-z]+)+$)[\\s\\-a-z0-9:/\\[\\]().%#]+$',
              // Identifiers, paths, URLs, mime types, storage keys.
              // camelCase identifiers only. A plain lowercase word must NOT be excluded
              // here: `saved`, `active` and `done` are all real UI copy, and a pattern of
              // `^[a-z][a-zA-Z0-9]*$` would swallow every one of them along with
              // `onClick` and `userId`. Requiring an interior capital keeps the
              // identifiers out and the words in.
              '^[a-z][a-z0-9]*[A-Z][a-zA-Z0-9]*$',
              '^[A-Z][A-Z0-9_]*$',
              '^[\\w.-]+/[\\w./-]*$',
              '^https?://',
              '^[.~]?/',
              // Tokens with no letters at all: separators, punctuation, symbols, numbers.
              // Written as an ASCII class on purpose. `[^\p{L}]` looks equivalent but a
              // JS regex without the `u` flag reads `\p{L}` as the character class
              // `[p{L}]`, so that pattern silently means "contains no p, {, L or }" —
              // which excluded most English prose and hid five of six strings in a
              // six-string probe file.
              '^[^A-Za-z]*$',
            ],
          },

          // Callee-based exemptions: the argument is not user-visible copy.
          callees: {
            exclude: [
              // Diagnostics and dev-only output.
              '^console\\.\\w+$', '^(Type)?Error$', '^URL(SearchParams)?$',
              // Style and test helpers.
              '^(css|cx|clsx|twMerge|cva)$',
              // Storage, telemetry and routing take machine keys.
              '(local|session)Storage\\.\\w+', 'navigate', 'track', 'emit',
              'querySelector(All)?', 'getElementById', 'createElement',
              'addEventListener', 'removeEventListener', 'matchMedia',
              // WebGL/DOM capability lookups take registry identifiers
              // (`WEBGL_lose_context`), which are mixed-case and so escape the
              // all-caps word exemption above.
              'getExtension',
              // HTTP and serialisation: header names, endpoints, content types.
              'fetch', '\\w*[Hh]eaders?\\.\\w+', 'JSON\\.\\w+', 'encodeURI(Component)?',
              'setAttribute', 'getAttribute', 'removeAttribute', 'classList\\.\\w+',
              // The translate functions themselves. Anchored: these are matched as
              // regexes, so a bare 't' would exclude every callee whose name contains
              // the letter t.
              '^i18nT$', '^t$',
            ],
          },

          // Attribute-based exemptions: machine-facing JSX attributes.
          'jsx-attributes': {
            exclude: [
              // `title` is deliberately NOT here: it renders as a tooltip, so it is
              // user-visible copy, not a machine value.
              'className', 'class', 'id', 'key', 'href', 'src', 'to', 'type',
              'name', 'role', 'rel', 'target', 'method', 'action', 'style',
              'data-\\w+', 'aria-(hidden|live|orientation|current|haspopup)',
              'autoComplete', 'inputMode', 'enterKeyHint', 'spellCheck',
              'viewBox', 'xmlns', 'fill', 'stroke', 'd', 'points', 'transform',
              'encType', 'accept', 'pattern', 'lang', 'dir',
            ],
          },

          // Object properties that hold machine values rather than copy.
          'object-properties': {
            exclude: [
              'id', 'key', 'navId', 'slug', 'type', 'kind', 'code', 'name',
              'className', 'icon', 'path', 'route', 'href', 'url', 'method',
              'event', 'variant', 'color', 'align', 'position', 'placement',
            ],
          },

          // `Trans` is excluded by the plugin already; these render markup, not copy.
          'jsx-components': { exclude: ['Trans', 'Markdown', 'code', 'pre', 'kbd', 'samp'] },
        },
      ],
    },
  },
]
