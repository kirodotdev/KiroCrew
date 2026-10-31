import tsParser from '@typescript-eslint/parser'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import reactHooksPlugin from 'eslint-plugin-react-hooks'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import approvalOneShotDecision from './eslint-rules/approval-one-shot-decision.js'
import { plugin as shadcn } from '@shadcn/lint'
import { REACT_COMPILER_FILES } from './react-compiler.config.mjs'

// The react-hooks rules backed by the React Compiler's own analysis, each of
// which reports code the compiler would bail on. They apply to the compiler's
// opt-in files only (react-compiler.config.mjs): the rest of the tree still
// carries hundreds of hits and is not compiled. set-state-in-effect is left
// out because it does not stop compilation.
const REACT_COMPILER_RULES = Object.fromEntries([
  'component-hook-factories', 'config', 'error-boundaries', 'gating', 'globals',
  'immutability', 'incompatible-library', 'preserve-manual-memoization', 'purity',
  'refs', 'set-state-in-render', 'static-components', 'unsupported-syntax',
  'use-memo', 'void-use-memo',
].map((rule) => [`react-hooks/${rule}`, 'error']))

const HLJS_IMPORT_RULE = {
  name: 'highlight.js',
  message: "Import the core build instead: `import hljs from '<relative>/utils/hljs'`. The full barrel pulls every bundled grammar into the eager bundle.",
  allowTypeImports: true,
}

// `no-restricted-syntax` selectors, split so a file-scoped block can keep the
// theme/overlay gates while dropping only the raw-`/api`-fetch gate (the fetch
// inventory block below re-specifies the rule with just NATIVE_UI_SELECTORS).
const NATIVE_UI_SELECTORS = [
  {
    selector: "JSXOpeningElement[name.name='select']",
    message: 'No native <select> — its popup is drawn by the OS and ignores the theme. Use SimpleSelect, SearchableSelect, SettingsSelect, or DropdownMenu. See website/docs/page-layout.md.',
  },
  {
    selector: "CallExpression[callee.name='createPortal'][arguments.1.object.name='document'][arguments.1.property.name='body']",
    message: 'Portal through <OverlayPortal> or useOverlayRoot() (src/components/overlayRoot.tsx), not document.body: a layer on document.body stays on screen over a remote Crew.',
  },
]

// A raw `fetch('/api/…')` skips the transport journal: a request that drops
// before any Response (`TypeError: Failed to fetch`) is never recorded, so an
// "Ask the agent" notice for it carries no route or method (issue #18723).
// Reach the typed `api.*` facade: its domain methods under `api/client/` are
// built on the journaled transport (jfetch + sendResponseAuthRecovery), so a
// component reads one of those instead of calling `fetch` itself. Two spellings: a string first argument, and a template literal
// whose FIRST quasi starts `/api/` (an interpolated base like
// `fetch(`${base}/api/x`)` — vanishingly rare here — is not caught).
const RAW_API_FETCH_SELECTORS = [
  {
    selector: "CallExpression[callee.name='fetch'] > Literal.arguments:first-child[value=/^\\/api\\//]",
    message: "No raw fetch('/api/…') — it bypasses the transport journal, so a network drop produces a bare Ask-agent report with no route or method. Use the typed api.* facade: add a domain method under api/client (built on the journaled transport) and call that. See issue #18723.",
  },
  {
    selector: "CallExpression[callee.name='fetch'] > TemplateLiteral.arguments:first-child > TemplateElement.quasis:first-child[value.raw=/^\\/api\\//]",
    message: "No raw fetch(`/api/…`) — it bypasses the transport journal, so a network drop produces a bare Ask-agent report with no route or method. Use the typed api.* facade: add a domain method under api/client (built on the journaled transport) and call that. See issue #18723.",
  },
]

// The standing inventory of files that still call `fetch('/api/…')` directly
// (issue #18723), each mapped to the EXACT number of raw API fetches it holds
// today. Exported so `src/test/rawApiFetchGate.test.ts` can enforce the
// shrink-only property on BOTH edges: a file whose count drops to 0 has migrated
// and must come OFF this map (the list cannot rot into a blanket exemption), and
// a file whose count RISES has gained a new raw fetch that slipped past the gate
// under its own exemption — a busy file like `CliPanel.tsx` cannot add one
// unnoticed. The app entries (apps/*) are MCP-app iframes on their own
// `credentials: 'same-origin'` trust boundary; they are inventoried for
// completeness so the gate is a hard zero everywhere else. `src/api/**` is NOT
// here — it defines/aliases the journaled transport and is exempted as a
// directory below. The count only ever decreases; never raise an entry to admit
// a new raw fetch.
export const RAW_API_FETCH_INVENTORY = {
  'src/apps/crew-companion/petBridge.ts': 1,
  'src/apps/design-critique/api.ts': 2,
  'src/apps/md-notebook/api.ts': 3,
  'src/apps/mochi/api.ts': 2,
  'src/apps/mochi/panel/panelBridge.ts': 30,
  'src/apps/mochi/pet/main.tsx': 1,
  'src/apps/mochi/pet/petBridge.ts': 4,
  'src/apps/mochi/src/mochiApi.ts': 11,
  'src/components/AutoNudgePopover.tsx': 2,
  'src/components/CliPanel.tsx': 2,
  'src/components/FileRenderers.tsx': 1,
  'src/components/McpAppFrame.tsx': 2,
  'src/components/TerminalCompletion.tsx': 1,
  'src/components/WebAppArtifactCard.tsx': 2,
  'src/components/chat-input/optimizer.ts': 1,
  'src/components/settingRef/useConfigSchema.ts': 1,
  'src/components/useMarkdownPanelFileState.ts': 4,
  'src/hooks/usePanelDocumentActions.ts': 1,
  'src/hooks/usePathKind.ts': 1,
  'src/hooks/useRefreshScheduler.ts': 2,
  'src/hooks/useRunInTerminalBridge.ts': 1,
  'src/lib/externalTerminal.ts': 1,
  'src/lib/linkMeta.ts': 1,
  'src/pages/ArtifactDeployPage.tsx': 1,
  'src/pages/knowledge/api.ts': 1,
  'src/pages/overview/PortabilityTab.tsx': 2,
  'src/shell/boot/terminalRestore.ts': 2,
}

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
        paths: [HLJS_IMPORT_RULE],
        // Radix primitives portal to document.body unless told otherwise; the
        // shared wrappers in components/ui/ route them through the owning
        // pane's overlay root and close them on a Crew switch
        // (src/components/overlayRoot.tsx). Import the wrapper, not the primitive.
        patterns: [{
          group: ['@radix-ui/react-*'],
          message: 'Use the shared wrapper in src/components/ui/: it portals into the owning pane\'s overlay root and closes on a Crew switch (src/components/overlayRoot.tsx).',
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
      // A browser-native confirm()/alert()/prompt() opens an OS-drawn dialog
      // that ignores every theme token and blocks the whole renderer thread.
      // The dashboard owns these decisions in-app: destructive choices go
      // through useConfirm() / ConfirmDialog, errors and acknowledgements
      // through the in-app notice and status surfaces. The window-qualified
      // forms (`window.confirm`) reach the same globals, so no-restricted-
      // properties pairs with no-restricted-globals to cover both spellings.
      //
      // 'error', not 'warn': like the native-<select> gate above this is a gate
      // on a set that only shrinks, so it stays out of the --max-warnings budget
      // where a real regression would be indistinguishable from unrelated noise.
      // The 35 modules that still call these globals are exempted by name in the
      // allowlist block below (see issue #15662); every other renderer file is
      // refused a new call. Electron main-process dialogs live outside src/ and
      // are untouched. Tests drive these globals through the harness and are
      // exempted alongside the native-<select> rule in the test blocks below.
      'no-restricted-globals': ['error',
        { name: 'confirm', message: 'No browser-native confirm() — use useConfirm() / ConfirmDialog. See issue #15662.' },
        { name: 'alert', message: 'No browser-native alert() — route through the in-app notice or status surface. See issue #15662.' },
        { name: 'prompt', message: 'No browser-native prompt() — use the native text-entry dialog. See issue #15662.' },
      ],
      'no-restricted-properties': ['error',
        { object: 'window', property: 'confirm', message: 'No window.confirm() — use useConfirm() / ConfirmDialog. See issue #15662.' },
        { object: 'window', property: 'alert', message: 'No window.alert() — route through the in-app notice or status surface. See issue #15662.' },
        { object: 'window', property: 'prompt', message: 'No window.prompt() — use the native text-entry dialog. See issue #15662.' },
      ],
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
      //
      // A floating layer portalled straight to `document.body` escapes the local
      // pane a remote Crew hides, so it stays painted over the other Crew.
      // OverlayPortal / useOverlayRoot resolve the owning pane's overlay root
      // instead (see src/components/overlayRoot.tsx).
      'no-restricted-syntax': ['error', ...NATIVE_UI_SELECTORS, ...RAW_API_FETCH_SELECTORS],
    },
  },
  {
    // Allowlist for no-restricted-globals / no-restricted-properties: the
    // renderer modules that still call a browser-native confirm/alert/prompt
    // today (issue #15662). The gate above is 'error' for every OTHER renderer
    // file, so a NEW native dialog anywhere else is refused; these 35 are the
    // standing inventory, migrated to useConfirm() / in-app surfaces in later
    // PRs. This list only SHRINKS: delete a path the moment its last native
    // dialog is gone, never add one. Keep it sorted.
    files: [
      'src/apps/auto-improvement/AutoImprovementPage.tsx',
      'src/apps/auto-research/ResearchLabPage.tsx',
      'src/apps/crew-companion/GalleryPanel.tsx',
      'src/apps/design-tweak/DesignTweakPage.tsx',
      'src/apps/meetings/MeetingView.tsx',
      'src/apps/meetings/MeetingsPage.tsx',
      'src/apps/mochi/src/renderer/GalleryPanel.tsx',
      'src/apps/papyrus/PapyrusPage.tsx',
      'src/apps/papyrus/ProjectList.tsx',
      'src/components/RestartButton.tsx',
      'src/components/TagManagerList.tsx',
      'src/components/WebAppArtifactCard.tsx',
      'src/components/themeEditor.tsx',
      'src/hooks/useKeyboardShortcuts.ts',
      'src/hooks/useSessionActions.ts',
      'src/pages/ArtifactDetailPage.tsx',
      'src/pages/ArtifactsPage.tsx',
      'src/pages/ChannelPage.tsx',
      'src/pages/ChatSidebar.tsx',
      'src/pages/ProjectsPage.tsx',
      'src/pages/knowledge/DetailView.tsx',
      'src/pages/knowledge/SourcesList.tsx',
      'src/pages/knowledge/index.tsx',
      'src/pages/members/MembersPage.tsx',
      'src/pages/members/NewCrewmateDialog.tsx',
      'src/pages/overview/AgentCfgTab.tsx',
      'src/pages/overview/AgentTemplatesTab.tsx',
      'src/pages/overview/KiroCrewCfgTab.tsx',
      'src/pages/overview/MemoryTab.tsx',
      'src/pages/overview/PortabilityTab.tsx',
      'src/pages/overview/PromptsTab.tsx',
      'src/pages/overview/SkillsTab.tsx',
      'src/pages/overview/SteeringTab.tsx',
      'src/utils/popoutController.ts',
    ],
    rules: {
      'no-restricted-globals': 'off',
      'no-restricted-properties': 'off',
    },
  },
  {
    // The transport layer itself. `api/client.ts` DEFINES the journaled `jfetch`
    // `jfetch`, and every `api/client/*` domain module destructures
    // `jfetch: fetch` from the injected ClientTransport — so a `fetch('/api/…')`
    // here is the journaled helper under a local alias, not the browser global.
    // The gate would read those as raw bypasses; drop it here (keeping the
    // native-<select> / document.body-portal gates) because this is the one
    // place the journaling actually lives.
    files: ['src/api/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...NATIVE_UI_SELECTORS],
    },
  },
  {
    // Allowlist for the raw-`/api`-fetch gate (issue #18723): the modules that
    // still call `fetch('/api/…')` directly today. For these, no-restricted-
    // syntax keeps the native-<select> and document.body-portal gates but drops
    // the fetch gate — so a NEW raw API fetch anywhere ELSE is refused, while
    // these migrate to the typed api.* facade in later PRs.
    //
    // This list only SHRINKS: delete a path the moment its last raw API fetch is
    // gone, never add one. Keep it sorted. The app entries (apps/*) are MCP-app
    // iframes on their own `credentials: 'same-origin'` trust boundary, not the
    // core session-key transport; they are inventoried here for completeness so
    // the gate is a hard zero everywhere else. `src/api/client.ts` is not listed
    // because it DEFINES the journaled transport (jfetch) the gate
    // steers callers toward.
    files: Object.keys(RAW_API_FETCH_INVENTORY),
    rules: {
      'no-restricted-syntax': ['error', ...NATIVE_UI_SELECTORS],
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
    // The shared Radix wrappers are the one place a primitive is imported, and
    // tests may mock a primitive directly (src/test/__mocks__/@radix-ui).
    files: ['src/components/ui/**/*.{ts,tsx}', 'src/**/*.test.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-restricted-imports': ['error', { paths: [HLJS_IMPORT_RULE] }],
    },
  },
  {
    // Test doubles are exempt: a `vi.mock` that swaps a portalled Radix dropdown
    // for a plain <select> is the ESTABLISHED way to make one driveable in jsdom
    // (Radix commits discrete events through flushSync, which throws inside
    // Testing Library's act() — see src/test/CrewEditorSelect.test.tsx). Nothing
    // here renders to a user. The native-dialog gate is off here for the same
    // reason: a test that stubs window.confirm to drive a flow is harness code.
    files: ['src/**/*.test.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': 'off',
      'no-restricted-globals': 'off',
      'no-restricted-properties': 'off',
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
    // no-restyle is NOT enabled here: the tree carries a few hundred call sites
    // that restyle ui/ components (DropdownMenuItem, TableCell, …) and CI runs
    // --max-warnings 0, so 'warn' would fail the build outright. Enabling it is a
    // design decision (fix the sites or write per-component contracts), not a
    // lint toggle. The backlog is held where it is by scripts/check-restyle-
    // ratchet.mjs, which runs the rule through its own config against a per-file
    // baseline that can only shrink. Keep that script's CONFIG in step with the
    // parser options and carve-outs of this block.
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
          // styles/message-font-size.css (imported in main.tsx, outside index.css's
          // @source graph): the bubble scope, the chip/text size classes, and the
          // marker MarkdownRenderer's link chips carry.
          'mc-message-font-*', 'mc-md-ref-chip',
          // styles/crew-notes.css (imported in main.tsx, outside index.css's
          // @source graph): the crewmate Notes-panel readability scope.
          'crew-notes',
          // components/crew/layout/layoutEditor.css (imported by LayoutEditor.tsx,
          // outside index.css's @source graph): the layout editor's Neo-Frame
          // "Bench" build-mode classes and their drag/drop/target state hooks,
          // all le-prefixed so no generic state name is exempted repo-wide.
          'le-*',
          // pages/chat/pinnedViews.css (imported by usePinnedViews.ts, outside
          // index.css's @source graph): the side panel strip's Pin Summary scope.
          'side-panel-strip-summary-pinned',
          // components/crewIconMotion.css (imported by CrewAvatar.tsx, outside
          // index.css's @source graph): a working icon avatar's body motion.
          'crew-icon-*',
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
  // ESLint refuses an empty `files` list, so the block exists only once a file
  // is listed.
  ...(REACT_COMPILER_FILES.length > 0 ? [{ files: REACT_COMPILER_FILES, rules: REACT_COMPILER_RULES }] : []),
]
