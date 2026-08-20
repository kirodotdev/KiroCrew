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
      // MODEL-FACING PROMPTS, by naming convention. A `*.prompt.ts` module may
      // contain ONLY the text of a message sent to an agent — no UI copy — so the
      // suffix IS the boundary and its sibling module stays fully covered. Same
      // category as the test files above: English by design, not suppressed debt.
      //
      // Translating a prompt would change agent BEHAVIOUR (the agent reads the
      // instructions and acts on them), not the interface language. It is still shown
      // to the user — the seed prompt is sent with `api.sendChat`, so it appears in the
      // transcript — which is why this is an explicit, named boundary rather than a
      // shape rule pretending the text is invisible.
      //
      // A `words.exclude` shape rule was tried first and cannot do this job. It IS
      // consulted for a template literal — eslint-plugin-i18next validates each quasi's
      // trimmed text (`no-literal-string.js` → `isValidLiteral` → `shouldSkip`) and only
      // reports at the whole node — but these quasis are ordinary English sentences, so
      // no regex covers them without also exempting genuine UI copy.
      'src/**/*.prompt.ts',
      // Color paint plumbing for the sidebar folder glyph: every string is a
      // CSS value template (color-mix over theme variables), never
      // user-visible copy. Same named-boundary idiom as `*.prompt.ts` above —
      // the module may contain ONLY paint data, so the filename IS the
      // boundary and its consumer (FolderGlyph.tsx) stays fully covered.
      'src/components/folderColorPaint.ts',
      // Pierre's shared render configuration: injected stylesheet text
      // (`unsafeCSS` templates of selectors, lengths and `var(--…)` references),
      // theme ids the library matches on, and an extension→grammar map. None of
      // it is read as words. Same named-boundary idiom as `folderColorPaint.ts`
      // above — the module may contain ONLY render config, and every Pierre
      // surface that shows copy (DiffBlock, MarkdownPanel, FileBrowserRail)
      // stays fully covered.
      //
      // Stated as a false-negative class, per this file's convention: user-visible
      // copy added here will not be reported. Verified copy-free rather than
      // assumed — it imports neither `i18nT` nor `useTranslation`.
      'src/pierre/config.ts',
      // Injected stylesheet text for the chat file-change chips: selectors,
      // lengths and keyframes handed to the CSS parser, with the two layout
      // numbers and the animation duration the rules interpolate. Extracted from
      // `FileChangeChips.tsx` precisely so the component -- which does carry
      // user-visible copy -- stays fully covered here.
      //
      // Stated as a false-negative class, per this file's convention: copy added
      // to this module will not be reported. Keep it stylesheet-only; anything a
      // person reads belongs in the component with `i18nT`.
      'src/components/fileChangeChipsCss.ts',
      // Synthesizes the `diff --git` / `---` / `+++` headers Pierre needs to
      // identify a file in a bare patch body: git wire format handed to Pierre's
      // parser, never read as words. Extracted from `PullRequestPanel.tsx` so that
      // panel -- which does carry copy -- stays fully covered.
      'src/components/unifiedPatchHeaders.ts',
      // Per-shell env-var export command builders for SettingRef's env popover:
      // every string is CLI syntax handed to a terminal (`export`, `$env:`,
      // `set`, `=1`), never user-visible copy — translating a fragment would
      // break the command. Same named-boundary idiom as `*.prompt.ts` above:
      // the module may contain ONLY command builders (shell display names live
      // in the catalog as `privacyDisclosure.shell*Label` keys), so the
      // filename IS the boundary and its consumer (SettingRef.tsx) stays fully
      // covered by the gate.
      'src/components/settingRef/envShellCommands.ts',
      // Generated and data-only.
      'src/i18n/locales/**',
      // Generated sources: the copy's real home is the panel that declares the
      // setting, which is scanned and baselined on its own. Editing a `.gen.ts`
      // is overwritten by `npm run gen:settings`, so a finding here is
      // unactionable AND a double count of a string already covered elsewhere.
      'src/**/*.gen.ts',
      // Model-facing prompt modules that do not carry the `*.prompt.ts` suffix
      // handled above: text handed to a MODEL, not user-visible copy. The skill
      // names, file paths and role framing they carry are English identifiers the
      // agent matches on, so translating them would degrade instruction-following
      // while changing nothing anyone sees. Each is declared by WHERE the string
      // lives rather than by a content regex — these files hold ONLY prompt text,
      // and their user-visible siblings stay fully gated.
      //
      // Stated as false-negative classes, per the rule this file follows
      // elsewhere: any user-visible copy ever added to these paths will not be
      // reported. None of them was reported before `i18n-strict` either (the
      // ALL-CAPS suppression already hid all of it), so this removes no coverage
      // that existed — it declines to add noise.
      'src/apps/*/companionPrompt.ts',
      'src/prompts/**',
      'src/apps/*/prompts.ts',
      // The Meetings sketch-frame srcdoc builder. Same rationale as the prompt
      // modules above, one step further from the user: every literal in it is
      // handed to a PARSER, never to a person — CSP directives, a DOCTYPE, the
      // frame's own CSS, and a fixed JS bootstrap. Translating any of them would
      // not change a word anyone reads, it would break the policy or the diagram.
      // The file carries no user-visible copy at all (its only strings shown to
      // anyone are the i18nT keys in AgentPanel.tsx, which stays fully gated).
      //
      // Deliberately ONE exact path rather than a `*Srcdoc.ts` glob or a
      // CSP-shaped content regex. A content regex was measured and rejected: a
      // `^(default|script|img|…)-src\b` exclusion retroactively drops
      // lib/mcpAppSrcdoc.ts 16 -> 8 and lib/widgetSrcdoc.ts 21 -> 17, and a
      // ratchet that silently hands back other files' debt is worse than the
      // false positive it fixes. A path this narrow cannot exempt a future file
      // that does hold copy.
      'src/apps/meetings/lib/sketchSrcdoc.ts',
      // The widget and MCP-app srcdoc builders — the same category as
      // `sketchSrcdoc.ts` directly above, and listed by the same exact-path rule
      // rather than a shared `*Srcdoc.ts` glob. Every literal in both is handed to
      // a PARSER: CSP directives, a DOCTYPE, `<meta>`/`<style>`/`<script>` markup,
      // the frame's own reset CSS, and two fixed JS bootstraps (the height reporter
      // and the nav beacon). Translating any of them would not change a word anyone
      // reads — it would break the policy or blank the frame.
      //
      // Verified copy-free rather than assumed: neither module imports `i18nT` /
      // `useTranslation`, neither renders a text node, and their exports are pure
      // builders. The consumers that DO show copy (`WidgetFrame.tsx`, the MCP app
      // host) stay fully gated.
      //
      // Two exact paths, not a content regex. The `^(default|script|img|…)-src\b`
      // exclusion that the `sketchSrcdoc.ts` note above records as tried-and-rejected
      // is exactly what would cover these, and it leaked into unrelated files; a
      // measured `^rgba?\(` variant leaked into `apps/issue-radar/lib/format.ts`.
      // A path this narrow cannot leak.
      //
      // Stated as a false-negative class, per this file's convention: any
      // user-visible copy ever added to these two paths will not be reported —
      // keep both modules parser-facing only.
      'src/lib/widgetSrcdoc.ts',
      'src/lib/mcpAppSrcdoc.ts',
      // Per-app scoped CSS, injected as `<style>{APP_CSS}</style>`. Each module is
      // ONE template literal of stylesheet text handed to the CSS parser -- selectors,
      // lengths and `var(--…)` references. None of it is read as words, and the
      // diff-scoped `added-lines` check reports the whole template against whoever
      // touches a rule inside it, so any narrow-viewport or theming edit to an app's
      // stylesheet trips a zero-tolerance gate it can never satisfy.
      //
      // Stated as a false-negative class, per this file's convention: user-visible
      // copy added to one of these modules will not be reported -- keep them
      // stylesheet-only, and put anything a person reads in the component with
      // `i18nT`. Verified copy-free rather than assumed: none of these four
      // imports `i18nT` or `useTranslation`.
      //
      // Listed as EXACT PATHS, not a `src/apps/*/styles.ts` glob, for the reason
      // stated above for the srcdoc pair: the false-negative note is only true of
      // files that exist today. A glob would put every future app's stylesheet
      // outside this gate sight-unseen, including one where someone later writes
      // `content: "…"` copy or misfiles a string. One config line per new app is
      // the cost of keeping the ratchet's shape.
      'src/apps/crew-companion/styles.ts',
      'src/apps/design-critique/styles.ts',
      'src/apps/file-explorer/styles.ts',
      'src/apps/md-notebook/styles.ts',
      // The PPTX Maker board-preview builder — the same category as
      // `sketchSrcdoc.ts` directly above, and listed by the same exact-path rule
      // rather than a shared glob. Every literal in it is handed to a PARSER: the
      // preview iframe's egress-denying CSP directives, the `<style>` reset that
      // neutralizes the engine's own page zoom, and two `class="slide"` match
      // patterns. Translating any of them would silently WEAKEN the policy or
      // blank the preview, not change a word anyone reads.
      //
      // Verified copy-free rather than assumed: the module imports no `i18nT` /
      // `useTranslation` and every remaining literal is a CSS length, a union-type
      // tag or a regex. Its user-visible siblings (`BoardFrame.tsx`,
      // `LibraryPanel.tsx`, `DeckViewer.tsx`) stay fully gated, so copy added to
      // the app still has to go through the catalog.
      //
      // Stated as a false-negative class, per this file's own convention: any
      // user-visible copy ever added to THIS path will not be reported — keep the
      // module parser-facing only.
      'src/apps/pptx-maker/lib.ts',
      // Emits runnable shell text, not copy: the output is pasted into a terminal
      // and executed, so translating a `curl` invocation, an `openssl` flag or a
      // header name would produce a snippet that fails. Exempted by exact path;
      // the Webhooks page itself remains fully gated.
      'src/pages/webhooks/requestExamples.ts',
      // Model-facing, not user-facing: `planningInstructionForMode` returns the
      // behaviour instruction embedded in the pet's planning PROMPT. Translating it
      // would send the agent a localized instruction while the rest of its prompt
      // stays English, which is a regression, not a fix.
      'src/apps/mochi/src/shared/config.ts',
      // Design tokens / pack DATA, not UI chrome: `themes.ts` is CSS custom
      // properties + color-mix values; `builtInCatPresets.ts` is a hex→region
      // color map; `builtinPacks.ts` is builtin pack definitions (ids, authors,
      // thumbnail filenames, brand names, and English-pinned persona descriptions
      // that double as the agent prompt). None of it is translatable UI copy.
      'src/apps/mochi/src/shared/themes.ts',
      'src/apps/mochi/src/shared/builtInCatPresets.ts',
      'src/apps/mochi/builtinPacks.ts',
      // Machine HTML/markup only (DOCTYPE, a fixed stylesheet, a sandboxed
      // iframe element) that hosts an untrusted widget in a browser popout —
      // never user-facing copy. Same rationale as the srcdoc builders above.
      'src/apps/mochi/src/shared/widgetPopout.ts',
      // Key glyphs / key-cap names only (⌘ ⇧ ⌥ / Ctrl Win Alt) — a machine
      // grammar the OS parses, not translatable copy. Same rationale as above.
      'src/apps/mochi/src/shared/shortcut.ts',
      // CSS text injected through <style>; a stylesheet is not translatable copy.
      'src/apps/spec-builder/inlineStyles.ts',
      // The theme stylesheet builders, extracted out of `hooks/useTheme.tsx` so
      // they COULD be exempted by path. Every literal in the module is handed to
      // the CSS parser: the `--*` custom-property allowlist, the
      // `[data-theme="custom-<slug>-<mode>"]` selectors, the static font/radius/
      // color-scheme defaults, `@font-face` blocks, and the `url()` values the
      // overrides-pipeline rewrites to pack asset routes. Translating any of them
      // would not change a word anyone reads — it would unstyle the theme or
      // silently weaken the §4.2/§5.1 selector scoper.
      //
      // Verified copy-free rather than assumed, and by a MECHANICAL boundary
      // rather than a reading: the module imports no `i18nT` / `useTranslation`,
      // and it does not touch the DOM at all — every export is data in, CSS
      // `string` out. The `<style>` tags, `document.head` writes and
      // `style.setProperty` calls all stayed in `useTheme.tsx`, so nothing here
      // has a path to the screen.
      //
      // This split is the whole reason a path exemption is admissible here.
      // `useTheme.tsx` also declares `THEMES` — 19 theme-picker display names —
      // so exempting THAT file would hand back real copy, which is what the
      // `object-properties: next` note above refuses. Extracting the CSS leaves
      // the 19 names in a still-gated file. Measured: `useTheme.tsx` 34 -> 17
      // (exactly the 17 CSS findings), the new module contributes 0, and no other
      // file's count moves.
      //
      // Stated as a false-negative class, per this file's convention: any
      // user-visible copy ever added to THIS path will not be reported — keep the
      // module parser-facing only, and keep it DOM-free, which is the property
      // that makes that easy to check.
      'src/hooks/themeCss.ts',
      // The Kiro-ghost avatar generator's art tables. Every literal in the module
      // is SVG handed to the SVG parser: the shipped mark's `d` path data, the
      // per-part fragments built from it, and the hex tile colors. Translating any
      // of them would not change a word anyone reads — it would emit a malformed
      // path and render a blank avatar.
      //
      // Verified copy-free rather than assumed, and by a MECHANICAL boundary: the
      // module's only import is a TYPE from `@dicebear/core`, it imports no `i18nT`
      // / `useTranslation`, and it does not touch the DOM — every export is data in,
      // SVG `string` out. Its consumer `components/CrewAvatar.tsx` stays fully
      // gated, and that component renders the avatar `alt=""` / `aria-hidden`
      // precisely because the crew name is shown as real translated text beside it.
      //
      // Stated as a false-negative class, per this file's convention: any
      // user-visible copy ever added to THIS path will not be reported — keep the
      // module parser-facing only, and keep it DOM-free.
      'src/lib/kiroGhostAvatar.ts',
      // Same rationale, different convention: this app keeps its seed prompts in a
      // dedicated `lib/prompts.ts` rather than a `*Prompt.ts` file. Also prompt
      // payload sent over the wire, never rendered.
      'src/apps/*/lib/prompts.ts',
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
      // This binding is NOT what runs over the tree: `eslint.i18n.strict.config.js`
      // derives from this array and swaps in `eslint-rules/i18n-strict.js`, and that
      // is the config `scripts/check-i18n-strings.mjs` executes. This one survives as
      // the shared OPTIONS below plus the reference definition of "what upstream
      // reports" — the per-file ceilings are the strict run's unmarked findings,
      // which `src/test/i18nStrictRule.test.ts` pins to exactly this rule's output.
      // Keeping it here is what lets the ALL-CAPS hole close without a bulk
      // `--update` re-snapshot of a ledger four open branches share.
      'i18next/no-literal-string': [
        'warn',
        {
          mode: 'all',
          'should-validate-template': true,

          // Content-based exemptions, applied wherever the string appears.
          words: {
            exclude: [
              // CSS transform functions built from numbers, e.g.
              // `translate(${x}px, ${y}px)` or `rotate(${deg}deg)`. These are style
              // values written into el.style.transform, not copy.
              // NOTE the safer alternative was preferred first — moving the value into a
              // stylesheet — and it is used everywhere it can be. It cannot be used for
              // per-frame animation, where the numbers come from a rAF loop and no
              // stylesheet can express them.
              //
              // FULL-STRING, not a prefix: the plugin compiles each entry with
              // `generateFullMatchRegExp`, which appends `$`. Written as a prefix this
              // matched only the bare `translate(` and exempted nothing else — the exact
              // trap `i18nLintExemptions.test.ts` was written to catch. Interpolation
              // splits one template into several literals, so the shapes that must match
              // are `scale(`, `translate(-50%, -50%) translate(`, `px) scale(`, `px, `
              // and `)`: a run of transform function names, CSS units, digits and
              // punctuation, and nothing else. Any other letter makes it prose again, so
              // real copy ('Preview (', 'Rotate the image') is still reported.
              String.raw`^(?:(?:translate|translateX|translateY|rotate|scale|scaleX|scaleY|matrix)\(|px|deg|[-\d.%,\s()])+$`,

              // A URL query built from an already-encoded value, e.g.
              // `${PATH}?id=${encodeURIComponent(x)}`. A request path is a server
              // contract; translating it would 404. Full-string for the same reason as
              // above; the literals that reach the linter here are `?id=`, `?since=`
              // and `&v=`.
              //
              // The leading character is `[?&]`, not `?` alone: a CONTINUATION
              // parameter is exactly the same server contract as the first one, and a
              // URL carrying two parameters has to spell one of them with `&`. The
              // shape stays just as tight — prose takes neither a leading `?`/`&` nor
              // a trailing `=`, so this still reports real copy.
              String.raw`^[?&][a-z_]+=$`,

              // The same server contract with a FIXED flag value baked in, e.g.
              // `&resolve=1`. The value class is a single digit or lowercase word
              // (`=1`, `=true`) — never a sentence — so prose still cannot match.
              String.raw`^[?&][a-z_]+=[a-z0-9]+$`,

              // A catalog KEY assembled at runtime, e.g.
              // `apps.crewCompanion.state.${slot}`. Translating a key would break the
              // lookup it performs — the value it resolves to is what gets translated.
              String.raw`^apps\.[A-Za-z]+\.[A-Za-z]+\.$`,

              // MIME type lists for a file picker's `accept`, e.g.
              // 'application/json,.json' or 'image/png,image/webp,.png'. These are a
              // browser API contract, not copy: translating one silently stops the
              // picker matching any file. Shape: a slash-bearing type or a dot-extension,
              // in a comma-separated list, with no spaces — which prose never has.
              String.raw`^(?:[a-z]+\/[a-z0-9.+*-]+|\.[a-z0-9]+)(?:,(?:[a-z]+\/[a-z0-9.+*-]+|\.[a-z0-9]+))*$`,

              // `window.open` feature strings. 'noopener,noreferrer' is a SECURITY
              // argument — translating it would drop the protection that stops the opened
              // page reaching back through window.opener. Same shape rule as above would
              // not catch it (no slash, no dot), so it is named explicitly.
              String.raw`^(?:noopener|noreferrer|_blank|_self)(?:,(?:noopener|noreferrer))*$`,

              // Identifier PREFIXES that get concatenated with an index to form a slot
              // id, e.g. 'extra_load_' + i. Snake_case with a trailing underscore is a
              // shape UI copy never takes, and translating it would rename the slot and
              // orphan the art already stored under the old id.
              String.raw`^[a-z][a-z0-9]*(?:_[a-z0-9]+)*_$`,

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
              // Tailwind ARBITRARY-VARIANT clusters, e.g. the shared touch-target
              // overrides in utils/touchActions.ts:
              // `[@media(hover:none)]:opacity-100 [@media(hover:none)]:[&_button]:p-2.5`.
              // The class shape above cannot cover these: its char class forbids
              // `@`, `&` and `_`, which are exactly what an arbitrary variant is
              // made of. Such constants live at module level under ALL-CAPS names,
              // so `i18n-strict` looks inside them.
              //
              // Deliberately NARROWER than the general class shape: every
              // space-separated token must BEGIN with a bracketed `@`-variant
              // (`[@media(...)]:` or `[@supports(...)]:`), so admitting this shape
              // admits no new prose — copy never opens with `[@`. A cluster
              // merely containing such a token alongside a plain word still
              // fails, because every token must match end to end.
              String.raw`^\[@(?:media|supports)\([^)\s]*\)\]:[^\s]+(?:\s+\[@(?:media|supports)\([^)\s]*\)\]:[^\s]+)*$`,
              // Tailwind ARBITRARY-VALUE clusters whose bracketed value carries a
              // comma or underscore, e.g. the notification glass surfaces in
              // components/notifications/NotificationFeed.tsx:
              // `bg-[color-mix(in_srgb,var(--card)_72%,transparent)] backdrop-blur-2xl`
              // or `shadow-[0_8px_24px_rgba(0,0,0,.10),0_1px_3px_rgba(0,0,0,.06)]`.
              // The general class shape above cannot cover these: its char class
              // forbids `,` and `_`, which are exactly what Tailwind's arbitrary-value
              // syntax uses to encode CSS commas and spaces inside `[...]`. Such
              // strings sit in plain `const` ternaries (not JSX attributes), so the
              // attribute exemption does not reach them either.
              //
              // Deliberately NARROWER than "allow , and _ anywhere", on two axes:
              // (a) the first lookahead rejects any two ADJACENT bare lowercase
              // words — the prose shape (`connection lost [retry_pending]`)
              // that would otherwise ride in on a single bracketed token; a
              // class cluster never has two adjacent bare words, every
              // utility next to a bare `border`/`isolate` carries a hyphen,
              // digit, colon or bracket. (b) the second lookahead requires at
              // least one space-free `[...]` token containing a `,` or `_` —
              // and `,`/`_` are admitted ONLY inside brackets; outside them
              // the char class is the general class shape's. A sentence
              // merely containing a bracket still fails, because its commas
              // live outside the brackets.
              //
              // Known false negative, stated: a SINGLE bare word plus
              // bracketed-value tokens (`saved bg-[color-mix(a,b)]`) would be
              // missed — the same single-word residue the general class shape
              // already accepts, caught by the en-XA render gate instead.
              String.raw`^(?!.*(?:^|\s)[a-z]+\s+[a-z]+(?:\s|$))(?=[^\[]*\[[^\]\s]*[,_][^\]\s]*\])(?:[\s\-a-z0-9:/().%#]|\[[\-a-z0-9:/().%#,_]*\])+$`,
              // A GATEWAY WIRE MARKER whose tag is bracketed ALL-CAPS, e.g.
              // `[SYSTEM] Sub-agent synthesis:`. These are matched byte-for-byte
              // with `startsWith` against Python constants in
              // src/kiro_crew/dashboard/state.py and the matched prefix is then
              // SLICED OFF, so no character reaches the screen — translating one
              // silently stops its card from rendering in that locale. Real site:
              // the `PREFIXES` table in pages/chat/RecoveryCard.tsx, an ALL-CAPS
              // module constant, so `i18n-strict` looks inside it.
              //
              // Deliberately narrow: the string must OPEN with `[`, the tag must be
              // ALL-CAPS (`[A-Z]+`), no second `[` may follow, AND it must END in a
              // colon — the shape a wire marker whose instruction continues on the
              // same line takes. The trailing colon is what keeps real copy out:
              // without it `[BETA] Experimental — expect changes` and `[ERROR] Unable
              // to load session` would both be exempt, which two reviewers flagged.
              // Known false negative, stated: copy that opens with a bracketed
              // all-caps tag AND ends in a colon is exempt. The wholly-bracketed
              // mixed-case siblings in that same table do not need this pattern —
              // measured: an existing shape already covers a string that is
              // entirely one bracketed token. This entry exists for the marker
              // that carries text AFTER the closing bracket, which that shape
              // cannot admit.
              String.raw`^\[[A-Z]+\][^\[\]]*:$`,
              // CSS SELECTOR LISTS, e.g. `[role="dialog"],[data-x]` or
              // `a,button,[tabindex]` — a comma-joined list of type selectors and
              // bracketed attribute selectors, as passed to querySelector. The
              // Tailwind/class shape above cannot cover these: its char class forbids
              // `=`, `"` and `,`, which is exactly what an attribute selector is made
              // of. Such constants live at module level under an ALL-CAPS name, so
              // `i18n-strict` looks inside them.
              //
              // A bare type selector is admitted only alongside a bracketed one: the
              // leading lookahead requires at least one `[` in the WHOLE string, and
              // that is what keeps this entry from becoming a general "lowercase words
              // joined by commas" exemption. Without it `'save,delete'` would match,
              // and `\s*,\s*` permits a space, so `'save, delete'` would too. A
              // sentence merely containing a bracket still fails, because every member
              // must match end to end and a prose member carries spaces.
              //
              // Known false negative, stated: a comma-joined list of lowercase words
              // that also holds a bracketed term is exempt. Copy does not take that
              // shape — a bracket in copy sits inside a phrase, not as a list member.
              '^(?=[^\\[]*\\[)(?:[a-z][a-z0-9]*|\\[[a-z\\-]+(?:[~|^$*]?=(?:"[^"]*"|\'[^\']*\'))?\\])(?:\\s*,\\s*(?:[a-z][a-z0-9]*|\\[[a-z\\-]+(?:[~|^$*]?=(?:"[^"]*"|\'[^\']*\'))?\\]))*$',
              // Identifiers, paths, URLs, mime types, storage keys.
              // camelCase identifiers only. A plain lowercase word must NOT be excluded
              // here: `saved`, `active` and `done` are all real UI copy, and a pattern of
              // `^[a-z][a-zA-Z0-9]*$` would swallow every one of them along with
              // `onClick` and `userId`. Requiring an interior capital keeps the
              // identifiers out and the words in.
              '^[a-z][a-z0-9]*[A-Z][a-zA-Z0-9]*$',
              // A CATALOG KEY, i.e. the dotted path `i18nT()` takes. Needed
              // because the gate's own recommended fix for a dynamic key is a
              // table of literal keys (`STATUS_LABEL_KEY`, `FILTER_LABEL_KEY`,
              // `EFFORT_LABEL_KEY`), and those tables live at module level under
              // an ALL-CAPS name. Before `i18n-strict` closed the ALL-CAPS hole
              // they were suppressed as a side effect; without this pattern the
              // gate would report the very shape it tells you to write.
              //
              // Deliberately narrow: every segment must start with a lowercase
              // letter and hold only word characters, and there must be at least
              // one dot. Real copy has spaces. Known false negative, stated:
              // a dotted lowercase token IS exempt everywhere, so
              // `'user.name'`-shaped copy would be missed — it is not a shape UI
              // copy takes.
              '^[a-z][a-zA-Z0-9]*(?:\\.[a-z][a-zA-Z0-9_]*)+$',
              '^[A-Z][A-Z0-9_]*$',
              // lowercase_snake, the third member of the identifier family above and
              // the one this codebase generates most: the backend is Python, so every
              // wire field, enum value and decision token arrives snake_case
              // (`trust_command`, `design_doc`, `pool_agent`). Without it those literals
              // are exempt only when something else happens to cover them — for a while
              // that was the broad comparison-callee exemption, which meant
              // `['trust_command', 'trust_base'].includes(decision)` went quiet for the
              // wrong reason and got noisy again the moment that exemption was narrowed.
              // Naming the shape puts the exemption where the argument actually lives.
              //
              // Known false negative, stated: snake_case copy would be missed. It is not
              // a shape UI copy takes — copy has spaces and capitals, which is what keeps
              // `['Save changes', 'Delete item']` reported.
              '^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$',
              // A `KeyboardEvent.code` SIDE-SPECIFIC MODIFIER identifier, i.e.
              // `AltRight`, `ControlLeft`, `MetaRight`, `ShiftLeft`. These are DOM
              // protocol values: they are compared against `e.code` and never
              // rendered — the strings a user sees for the same keys come from the
              // catalog (`ptt_key_right_option` and friends). Such lists live at
              // module level under an ALL-CAPS name (`SELECTABLE_BARE_CODES`), so
              // `i18n-strict` looks inside them.
              //
              // Enumerated rather than shaped, deliberately. The obvious shape,
              // `^[A-Z][a-zA-Z0-9]*$` (PascalCase single token), was tried and
              // rejected: it also exempts `'Save'`, `'Delete'` and `'Done'`, which
              // are exactly the single-word copy the config already calls out as
              // the hardest false-negative class. Spelling out the eight members of
              // a closed DOM set cannot match prose — no English phrase is
              // `AltRight` — and a new key code has to be added here on purpose.
              '^(?:Alt|Control|Meta|Shift)(?:Left|Right)$',
              // A `mc:`-NAMESPACED BROWSER-STORAGE KEY, e.g.
              // `mc:notif:activeKinds:v2`, `mc:notif:seenChannels`. The dashboard
              // namespaces every localStorage key it owns under `mc:`, and such
              // keys live at module level under an ALL-CAPS name, so
              // `i18n-strict` looks inside them. None of the identifier patterns
              // above reach them: the camelCase one forbids colons, and the
              // Tailwind/class shape forbids the interior capital that
              // `activeKinds` has.
              //
              // Deliberately anchored on the app's own prefix rather than a
              // generic "has a colon" shape: UI copy never begins `mc:`, and the
              // char class forbids spaces, so prose cannot match even when it
              // contains a colon. Naming the shape here also retires the same
              // debt on the keys that predate this entry, instead of leaving each
              // one to a per-file ceiling that hides it.
              '^mc:[A-Za-z0-9:._-]+$',
              // The COMPANION's own browser-storage prefix, `cc:` — e.g.
              // `cc:pendingCursor`, `cc:lastStats`. Exactly the `mc:` case above, for
              // the app's own namespace: these live at module level under an ALL-CAPS
              // name, which is where `i18n-strict` looks inside, and the camelCase
              // pattern cannot reach them because it forbids the colon.
              //
              // A separate entry rather than widening the `mc:` one to any prefix: a
              // generic "word, colon, no spaces" shape would start exempting real copy
              // the moment a label contains a colon, which several already do
              // ("Missing:", "Preset name:").
              '^cc:[A-Za-z0-9:._-]+$',
              '^[\\w.-]+/[\\w./-]*$',
              // EVERY PATTERN IN THIS FILE IS MATCHED FULL-STRING, so a prefix
              // pattern MUST spell out its own tail. `eslint-plugin-i18next` compiles
              // each entry with `generateFullMatchRegExp`, which appends `$` unless the
              // source already ends in one:
              //
              //   `^https?://`  ->  /^^https?:\/\/$/    matched ONLY the bare scheme
              //   `^[.~]?/`     ->  /^^[.~]?\/$/        matched ONLY `/`, `./`, `~/`
              //
              // Both were written as prefixes and so exempted nothing. The URL/path
              // class was silently carried by the lowercase-token pattern above
              // instead, whose character class holds no `*`, `?`, `_`, `=`, `&` or
              // capital — so `/api/chat/*`, `/api/file_read`, `/api/chat?slot=1` and
              // `https://kiro.dev/Docs` were all reported as untranslated copy. That is
              // invisible on existing code (frozen in the ledgers, and both whole-repo
              // ledger checks are report-only) and fails only NEW code, at
              // `[added-lines]`/`[vs-base]` zero tolerance — a hole that bites none of
              // the authors who caused it and every author who arrives later. Repairing
              // the two patterns drops 53 findings tree-wide and adds none.
              //
              // `\S*` rather than a character class: a leading `/` or a scheme plus no
              // whitespace is not a shape UI copy takes, and enumerating URL characters
              // is what produced the hole. Known false negative, stated: a one-word
              // slash-prefixed string (`'/Delete'`) is exempt.
              '^https?://\\S*$',
              '^[.~]?/\\S*$',
              // A FILE-PICKER `accept` EXTENSION LIST, e.g.
              // `,.txt,.md,.json,.har,.yaml` — the comma-joined dot-extension
              // string handed to `<input type="file" accept=…>`. These live at
              // module level under an ALL-CAPS name (`FILE_ACCEPT`), so
              // `i18n-strict` looks inside them, and no identifier pattern above
              // reaches them: the dotted-token shape requires a leading letter
              // and the path shapes require a slash. The string is DOM protocol
              // data — the browser matches it against filenames and no character
              // of it is rendered as copy; translating a fragment would break
              // the picker's filtering.
              //
              // Deliberately narrow: an optional LEADING comma (the literal is
              // concatenated after a MIME list), then one-or-more comma-joined
              // `.lower09` tokens, full-string. Prose cannot match — every token
              // must begin with a dot and the char class holds no spaces or
              // capitals. Known false negative, stated: a string that is ONLY
              // dot-extensions (`'.har'`) would be exempt anywhere — it is not a
              // shape UI copy takes.
              '^,?\\.[a-z0-9]+(?:,\\.[a-z0-9]+)*$',
              // A GATEWAY WIRE MARKER, e.g. `[Tool refusal — automatic recovery]` or
              // `[Continue — requested by the user]`. These are matched with
              // `startsWith` against gateway-authored transcript rows and must stay
              // BYTE-IDENTICAL to the Python constants in
              // `src/kiro_crew/dashboard/state.py`; the matched prefix is then SLICED
              // OFF, so no character of it ever reaches the screen — the card's visible
              // copy comes from `i18nT()`. Translating one would silently stop every
              // recovery card from rendering in that locale, which is the failure this
              // exemption exists to prevent: without it the gate pushes a contributor
              // toward "fixing" a wire value by translating it.
              //
              // Narrow by SHAPE, not by file: bracket-delimited end to end, and it must
              // carry a spaced em dash. UI copy is neither bracketed nor em-dash-joined,
              // and the bracketed CSS attribute selector covered above has no em dash.
              '^\\[[A-Za-z][A-Za-z0-9 ]* — [A-Za-z0-9 ]+\\]$',
              // The same class of wire marker without an em dash. ENUMERATED, not
              // shaped: the thing this protects is a small closed set of named
              // constants, and a shape like "bracketed capitalized words" would
              // also exempt a future hardcoded placeholder (`[No results found]`),
              // shipping it untranslated to every locale without tripping the
              // gate. Adding a marker here is a deliberate one-line act, which is
              // the right cost for adding one to the wire protocol.
              '^\\[(Subagent|Subagent batch|Workflow) completion event\\]$',
              // The Stop-hook nudge-cap backstop marker (no em dash), enumerated
              // for the same closed-set reason as the completion-event markers
              // above. Matched with `startsWith` and sliced off before render;
              // byte-identical to HOOK_HALTED_RECOVERY_PREFIX in state.py.
              '^\\[Stop-hook nudge cap reached\\]$',
              // NOTE ON SHAPE: the plugin wraps every pattern as `^<pattern>$`
              // (`generateFullMatchRegExp`), so a pattern must describe the WHOLE
              // string. A prefix-only pattern like `^data:` becomes `^^data:$` and can
              // never match — which is why each entry below is a full match.
              //
              // Data URIs and MIME types. A base64 payload is not copy, and the
              // `image/png` half of one is a wire value the server matches on. The
              // second pattern also covers a comma-joined accept list.
              'data:[\\s\\S]*$',
              '[a-z]+/[a-z0-9.+-]+(?:,[a-z]+/[a-z0-9.+-]+)*$',
              // Hex colours, and CSS functional values the lowercase-CSS pattern above
              // misses: a hex colour carries uppercase letters, and `scaleX(-1)` /
              // `rgba(...)` / `radial-gradient(...)` carry an interior capital or a comma.
              '#[0-9A-Fa-f]{3,8}$',
              '(?:scale|translate|rotate|skew|matrix)[XYZ]?\\([\\s\\S]*\\)$',
              '(?:rgba?|hsla?|var|calc|url|(?:linear|radial|conic)-gradient)\\([\\s\\S]*\\)$',
              // Multi-value CSS shorthand: `border-color 150ms, background 150ms`. A CSS
              // unit is required so ordinary prose containing a comma cannot slip through.
              '[a-z][a-z0-9-]*(?:[ ,][a-z0-9%.()-]+)*(?:ms|s|px|em|rem|%)(?:[ ,][^A-Z]*)?$',
              // snake_case discriminants: `trust_reads`, `not_connected`, `mochi_off`,
              // `approval_required`. The camelCase pattern above covers the other
              // identifier convention; this covers the one the pet's state and event
              // vocabularies use. An interior underscore is required, so a plain word
              // stays flagged.
              '[a-z][a-z0-9]*(?:_[a-z0-9]+)+$',
              // Dunder sentinels used as wire markers (`__approval__`, `__new__`), and
              // the DOM's own `_blank` / `noopener` window-feature tokens.
              '__[a-z][a-z0-9_]*__?$', '_(?:blank|self|parent|top)$', '(?:noopener|noreferrer)$',
              // Electron global-shortcut accelerators — a machine grammar the OS parses:
              // `CommandOrControl+Shift+M`.
              '(?:Command|Control|CommandOrControl|Cmd|Ctrl|Alt|Option|Shift|Super)(?:\\+[A-Za-z0-9]+)+$',
              // Key CAP names and modifier glyphs. These name physical keys, which the
              // catalog's own translator context says are left as printed on the keyboard
              // (see `components.shortcutsModal.k`, `components.commandPalette.tab`).
              '[⌘⇧⌥⌃]+[A-Za-z0-9]?$', '(?:Ctrl|Cmd|Alt|Win|Opt|Shift|Esc|Tab|Enter|Del)$',
              // A TEMPLATE LITERAL is validated one QUASI at a time (the rule reports
              // the whole template if ANY quasi fails), so the fragments BETWEEN
              // interpolations need shapes of their own. `data:${mime};base64,${b64}`
              // splits into `data:`, `;base64,` and `` — the first matches the data-URI
              // pattern above, the second needs this.
              '[;,]?base64,?$',
              // API paths with an optional query string: `/api/file-raw?path=`. The
              // `^[.~]?/` entry above cannot match these — the plugin full-anchors every
              // pattern, so a prefix-only one is inert.
              '/[\\w./-]*(?:\\?[\\w=&%-]*)?$',
              // The attachment wire format a composer writes into the outgoing message,
              // mirroring core's own convention (`[attached_file N] /path`,
              // `[attached_dir N] /path`, `![image](path)`).
              // Machine syntax the agent parses, not copy.
              '!\\[image\\]\\($', '\\[attached_(?:file|dir)$',
              // An escaped newline joining two interpolations. Quasi values are
              // TRIMMED before matching, so this arrives as the two characters
              // backslash and `n` — which the letterless pattern below cannot cover.
              '\\\\n$',
              // An HTML/SVG tag fragment used for content sniffing (`'<svg'`).
              '</?[a-z][a-z0-9]*$',
              // Tokens with no letters at all: separators, punctuation, symbols, numbers.
              // Written as an ASCII class on purpose. `[^\p{L}]` looks equivalent but a
              // JS regex without the `u` flag reads `\p{L}` as the character class
              // `[p{L}]`, so that pattern silently means "contains no p, {, L or }" —
              // which excluded most English prose and hid five of six strings in a
              // six-string probe file.
              '^[^A-Za-z]*$',
              // The product brand. The display name is `Kiro Crew`; the unspaced
              // `KiroCrew` is the same name (and the glossary term that still guards the
              // generated Slack app name), so both are equally DNT. Anchored
              // to the whole value, so a sentence merely *containing* the brand is still
              // reported — only the bare name is exempt.
              '^Kiro ?Crew$',
              // The messaging-channel product brands. Same class as the product
              // brand above and covered by the do-not-translate glossary: "Slack"
              // is "Slack" in every locale, and a localized spelling would name a
              // product that does not exist. They reach the UI as a folder-name
              // placeholder and an interpolated `{{channel}}` value in the
              // per-channel settings panels. Enumerated and whole-value-anchored,
              // so a sentence merely mentioning a channel is still reported —
              // only the bare name is exempt.
              '^(Slack|Discord|Telegram|Teams|Webex|WeCom|WeChat)$',
              // The PPTX Maker chat-token KEYWORDS (`[Style: name]`,
              // `[Template: name]`). Enumerated and whole-value-anchored, exactly like
              // the modifier-key caps below: the agent prompts parse this literal
              // spelling, so translating either word would render a token the agent
              // does not recognise — the do-not-translate case `website/AGENTS.md`
              // states ("a literal token the user must type must never be a catalog
              // value"). Two exact words cannot match ordinary copy, and no other file
              // changes count (measured).
              '^(Style|Template)$',
              // Physical modifier key caps, chosen by platform (`isMac ? '⌘' : 'Ctrl'`).
              // The glyph half is already exempt for having no letters; this exempts the
              // spelled half on the same do-not-translate grounds `en.context.json`
              // states for `Tab`, `Esc` and `K` — the string names a key the user
              // presses, so translating it would mislabel their keyboard. Anchored and
              // enumerated, not a pattern: ordinary copy cannot match it.
              '^(Ctrl|Alt|Shift|Cmd|Win)$',
              // Wire-protocol marker, not copy: the backend stamps `QUEUED:<fp>` onto a
              // `pr` value that was queued rather than drafted
              // (`spine/profile.py`: `return f"QUEUED:{fingerprint}"`), and the client only
              // ever `startsWith()`-matches it. Translating it would break the match — the
              // string is compared, never shown. Anchored with the colon so it cannot
              // swallow the word "queued" used as prose.
              '^QUEUED:$',
              // Persisted IDENTITY, not copy: this prefix builds the chat-folder NAME that
              // is also the lookup key (`folders.find(f => f.name === name)`, because there
              // is no upsert endpoint). Translating it would make a language switch fail to
              // find the existing folder and silently create a second one per language,
              // orphaning every prior session. Anchored WITHOUT the trailing space: the
              // plugin trims the literal before matching (`no-literal-string.js`: `const
              // trimed = value.trim()`), so a pattern that requires the space can never
              // match. Verified — the space-bearing version left the warning in place.
              '^Auto-Improve -$',
              // Electron accelerator API tokens, which are the INPUT side of the key
              // caps above: `accelerator: "CmdOrCtrl+R"` is the string Electron parses
              // to bind the shortcut, and the Windows titlebar menu rewrites those
              // tokens to the cap the user actually sees (`CmdOrCtrl` -> `Ctrl`). The
              // token never reaches the screen, so it is a machine value; the cap it
              // becomes is already exempt on do-not-translate grounds. Translating the
              // token would break the binding, not localise anything.
              //
              // Anchored and enumerated rather than a PascalCase shape rule on purpose:
              // `^[A-Z][a-z]+$` would also swallow `File`, `Edit` and `Settings`, which
              // are genuine UI copy.
              '^(CommandOrControl|CmdOrCtrl)$',
            ],
          },

          // Callee-based exemptions: the argument is not user-visible copy.
          callees: {
            exclude: [
              // Diagnostics and dev-only output.
              '^console\\.\\w+$', '^(Type)?Error$', '^URL(SearchParams)?$',
              // `popoutController.ts`'s two console shims: `logDebug` is
              // `console.debug` and `logWarn` is `console.warn`, both behind a debug
              // flag. Identical class to `^console\.\w+$` one line up — the argument
              // is operator diagnostics ("direct focus of … vetoed"), never a string
              // the UI renders.
              //
              // A CALLEE exemption and deliberately NOT a whole-file one: the module
              // also raises a `window.alert(i18nT(…))`, so releasing the file would
              // release a module that does render copy — it would fail the
              // "verified copy-free" standard the exact-path precedents above meet.
              //
              // Known false negative, stated: a future `logDebug`/`logWarn` defined
              // in another module inherits this. Both names exist in exactly one
              // module today (`src/utils/popoutController.ts`).
              '^log(Debug|Warn)$',
              // Validator diagnostics, for parity with `Error` above. A rejected input's
              // reason names the FIELD that failed (`Missing or invalid "meta" field`,
              // `Invalid meta.format: "…" (expected "svg", "lottie", or "sprite")`) and
              // is addressed to whoever authored the malformed file. The same sentence
              // passed to `new Error(...)` is already exempt one line up, so treating it
              // as copy only because it lands in an array instead of a throw would be an
              // artifact of the sink, not a statement about the text.
              '\\berrors\\.push$',
              // Style and test helpers.
              // `cn` is this repo's own `twMerge(clsx(...))` wrapper (src/lib/utils.ts)
              // and takes `ClassValue[]` -- the same category as the two helpers it
              // composes, both already listed here. Copy cannot legitimately reach it.
              '^(css|cx|clsx|twMerge|cva|cn)$',
              // Storage, telemetry and routing take machine keys.
              '(local|session)Storage\\.\\w+', 'navigate', 'track', 'emit',
              // KiroCrew's own telemetry shim (`src/rum.ts`). Its first argument is
              // a machine event name (`notifications_open`) and its second a tag
              // bag (`{ source: 'topbar' }`) — never rendered, so never copy. Sits
              // beside `track`/`emit` above, which are the same shape.
              '^recordEvent$',
              // Config PATCH takes a dotted config path (`telemetry.beacon_enabled`),
              // a machine key that must never be translated.
              'patchConfig',
              // Pet telemetry and state reporting: the argument is an event name from a
              // fixed vocabulary (`message_sent`, `tool_call`, `approval_required`), read
              // by the behaviour state machine, never rendered.
              '^report[A-Z]\\w*$', '^pickFile$',
              'querySelector(All)?', 'getElementById', 'createElement',
              'addEventListener', 'removeEventListener', 'matchMedia',
              // WebGL/DOM capability lookups take registry identifiers
              // (`WEBGL_lose_context`), which are mixed-case and so escape the
              // all-caps word exemption above.
              'getExtension',
              // A regex source is a pattern, never copy. Needed for natural-language
              // parsers, whose patterns are literals in the language they parse
              // (`每(?:隔)?…`) and so look exactly like untranslated user text.
              '^RegExp$',
              // HTTP and serialisation: header names, endpoints, content types.
              'fetch', '\\w*[Hh]eaders?\\.\\w+', 'JSON\\.\\w+', 'encodeURI(Component)?',
              // Notes app HTTP wrappers around `fetch`: their string args are the
              // method verb and the endpoint path (`/note?path=…`), never copy —
              // same class as `fetch` directly above. Uniquely named so the
              // exclusion cannot mask a `call(...)`/`vq(...)` callee elsewhere.
              '^mdnbCall$', '^mdnbVaultQuery$',
              // App-local request helpers. Their first argument is an endpoint path
              // (often a template literal carrying a query string), which is the
              // same machine value `fetch` above is excluded for.
              '^(get|send)JSON$',
              'setAttribute', 'getAttribute', 'removeAttribute', 'classList\\.\\w+',
              // STRING COMPARISON. The argument is the value being compared AGAINST,
              // so that call cannot render it — the same reason the plugin already
              // exempts `x === 'lit'` and `switch (x) { case 'lit': }` by position.
              // Without this, `err.startsWith('backing off')` is reported while the
              // byte-identical `err === 'backing off'` two lines away is not, purely
              // because one is a CallExpression.
              //
              // This is a POSITION exemption, not a shape one, and that is the whole
              // point: `'backing off'` as a wire-protocol token and `'backing off'` as
              // a label are the same string, so no content regex can separate them.
              // Position can — and the argument of `.startsWith()` is decidable.
              //
              // Scope note: a literal assigned to a named constant first and compared
              // later is still reported, correctly — nothing at the declaration says it
              // is a token rather than copy. Declaring the type
              // (`const X: Wire = 'backing off'`) is how an author says that, and the
              // plugin honours it via `getContextualType` — but only under a type-aware
              // parser, which this config deliberately does not pay for.
              //
              // ANCHORED, and not the bare method names, because a callee exemption
              // suppresses the WHOLE call subtree, receiver included. The plugin pushes
              // the callee verdict on `CallExpression` enter and tests the stack with
              // `.some()`, and `withDottedPrefix` compiles a bare `includes` to
              // `/^(?:.*\.)?includes$/` whose `.*` absorbs anything before the dot. With
              // the bare names, an inline table of UI copy tested for membership passed
              // at zero tolerance:
              //
              //   ['Save changes', 'Delete item'].includes(x)      // 0 findings
              //   (c ? 'Save changes' : 'Delete item').startsWith(s)
              //   g('Save changes').includes(x)
              //
              // Requiring the receiver to be an identifier/property chain reports all
              // three again. `(?:\(\))?` admits a zero-argument link
              // (`text.trim().startsWith(…)`) because empty parens cannot hold a
              // literal, while `g('Save changes')` still cannot match. `\s*` is there
              // because the plugin matches the callee's SOURCE TEXT, so a chain the
              // formatter broke across lines carries newlines — without it the gate
              // would depend on line width.
              //
              // Known false positives, stated: a receiver that is not a plain chain is
              // reported even when the call is a genuine comparison — `(a || b)`,
              // `(await p)`, `(x as string)`, `arr[0]`, `o['k']`, `s.slice(0, 3)`,
              // `a.filter(Boolean)`, or a comment spliced mid-chain. None occurs in
              // `src/` today with a reportable literal. Widening to admit them means
              // admitting `(…)` and `[…]`, which is the hole itself, so the cost is
              // deliberately left on the false-positive side — the same trade the
              // lowercase-token pattern above documents.
              '^[\\w$]+!?(?:\\s*\\??\\.\\s*[\\w$]+!?(?:\\(\\))?)*\\s*\\??\\.\\s*'
                + '(?:startsWith|endsWith|includes|indexOf|lastIndexOf|localeCompare)$',
              // The translate functions themselves. Anchored to the bare name: the
              // plugin wraps every callee pattern as `^(?:.*\.)?<pattern>$`, so an
              // UNANCHORED entry already matches the whole callee text (`fetch` and
              // `api.fetch`, never `prefetch`). A leading `^` opts out of the dotted
              // prefix, which is what is wanted here — `i18nT`, not `obj.i18nT`.
              '^i18nT$', '^t$',
              // Icon component factory: the string argument is a React DevTools
              // displayName, not user-visible copy.
              '^makePanelIcon$',
              // Built-in surface fallback labels and group buckets are registry
              // machine values. The helper is deliberately explicit and narrow;
              // rendered badgeLabel/activityLabel strings never pass through it.
              '^surfaceMachineValue$',
              // A per-app `request` wrapper takes an ENDPOINT PATH — the same class as
              // the `fetch` exclusion above, and the only thing standing between a
              // route string and the fetch it performs. Anchored, so it cannot match a
              // `requestSomething` that returns copy.
              '^request$',
            ],
          },

          // Attribute-based exemptions: machine-facing JSX attributes.
          'jsx-attributes': {
            exclude: [
              // `title` is deliberately NOT here: it renders as a tooltip, so it is
              // user-visible copy, not a machine value.
              'className', 'class', 'id', 'key', 'href', 'src', 'to', 'type',
              'name', 'role', 'rel', 'target', 'method', 'action', 'style',
              // A dotted config path (`path="session.pool_agent"`) addressing a key in
              // `config.json`, not copy. Already exempt as an object property below; a
              // JSX attribute of the same name carries the same machine value.
              'path',
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
              // Monaco tokenizer state transitions: `next: '@displayMath'`, `'@pop'`.
              // A grammar directive naming another rule in the same state machine,
              // never copy.
              //
              // Still deliberately NARROW. The wider set that would also fit the
              // rationale (`token`, `keywords`, `defaultToken`, …) was measured and
              // rejected: it retroactively drops AppIcon.tsx 4 -> 2, ChatPage.tsx
              // 25 -> 23, fileTokens.ts 5 -> 4 and NotificationDetailPanel.tsx 1 -> 0.
              // A ratchet that silently hands back unrelated files' debt is worse than
              // the false positive it fixes, so each of those needs its own decision,
              // not this one's coattails.
              'next',
              // `aliases: ['LaTeX', 'latex', 'BibTeX']` — the display names Monaco's
              // language REGISTRY matches against when resolving a language by name.
              // Not copy: they are looked up by value, and translating "LaTeX" into
              // nine languages would break the lookup while naming a format whose
              // wordmark is the same in every locale.
              //
              // An earlier revision of this comment recorded `aliases` as tried and
              // rejected alongside the wider set. That measurement was taken when
              // `latexLanguage.ts` was already in the baseline, where the two strings
              // cost one frozen ledger entry and exempting them was not worth a config
              // change. It no longer applies: the file is NEW, so the zero-tolerance
              // [added-lines] check governs instead and there is no baseline to carry
              // them. Re-measured under the same standard the wider set was rejected
              // for — `aliases` moves _total 1842 -> 1840 and changes no other file's
              // entry, so it hands nothing back.
              'aliases',
              // `error` on a VALIDATION RESULT object (`{ ok: false, error }`) — the
              // same class as `errors.push` in `callees` above, and exempt for the same
              // reason. A user-facing failure message belongs in a toast or a rendered
              // element, both of which are still gated.
              'error',
              // `reason` carries a diagnostic detail string, not copy: it explains WHY a
              // validator rejected a machine input (a capture region outside the screen
              // union, a malformed manifest field) and is logged or attached to a
              // result object rather than rendered as a sentence to the user.
              'reason',
              // CSS-in-JS style values: a grid template or font stack is a
              // stylesheet declaration, never copy.
              'gridTemplateColumns', 'fontFamily',
            ],
          },

          // `Trans` is excluded by the plugin already; these render markup, not copy.
          'jsx-components': {
            // `style` holds a stylesheet: the pet windows are separate bundles that
            // inline their own keyframes, so the child of a <style> tag is CSS source.
            exclude: ['Trans', 'Markdown', 'code', 'pre', 'kbd', 'samp', 'style'],
          },
        },
      ],
    },
  },

  // Debug-only developer diagnostics: text that goes to the browser console for
  // whoever is profiling, never to a user through the UI. The module is inert
  // unless explicitly armed with `?profile=commits`, and translating console
  // output would mean shipping ten locales of strings no user can reach.
  //
  // Scoped to this one file rather than widened globally: a `words.exclude` shape
  // rule cannot express "prose, but only in this module", and turning the rule off
  // for `src/lib/**` would silence real copy in its neighbours.
  {
    files: ['src/lib/commitProfiler.tsx'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // A pure-CSS module: one template literal of scoped style rules (hover/focus
  // states inline styles cannot express) injected via a <style> tag. It contains
  // selectors and declarations, never user-visible copy. The Tailwind/CSS shape
  // exemption cannot cover it — full CSS rules carry `{`/`}`/`;`, and widening
  // that character class would also exempt comma-joined prose like
  // `'no results, try again'`. Scoped to this one file for the same reason as
  // `commitProfiler.tsx` above: a shape rule cannot express "CSS, but only in
  // this module", and any copy later added to this file belongs in the catalog,
  // not here — keep this module CSS-only.
  {
    files: [
      'src/apps/md-notebook/styles.ts',
      // Same class as Notes: a CSS-in-TS string injected via <style>, never copy.
      // Editing a selector inside CC_CSS otherwise fails [added-lines] because the
      // whole template sits under an ALL-CAPS declarator.
      'src/apps/crew-companion/styles.ts',
    ],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // Developer diagnostics for the APP AUTHOR, printed to the browser console when
  // an app subscribes to a WS event its manifest has not declared a scope for.
  // Translating them would be actively wrong, not merely wasteful: each one quotes
  // the scope identifier the author must paste into `permissions.events`
  // (`"slots:user"`, `"notification:system"`, `"<scope>:all"`), and those are
  // compared BY VALUE against the manifest — localised advice would name a scope
  // the gateway does not recognise.
  //
  // `console.*` is already callee-exempt, so the three call sites are covered; the
  // strings are flagged because they are composed in `checkSubscribeAllowed`, one
  // pure predicate that centralises the diagnosis for all three. Inlining the prose
  // into the calls to earn the callee exemption would duplicate its branch logic
  // three times — a worse module for a lint technicality.
  //
  // Scoped to this one file for the same reason as the two above: the module is the
  // SDK's protocol surface (event tables, hooks, provider) and holds no other prose.
  // The pieces that DO render copy — `ChatEmbed`, `ChatPanel`, `ChatMessageList` —
  // are separate files and stay covered. Copy added here later belongs in the
  // catalog, not under this exemption; keep this module protocol-and-diagnostics.
  {
    files: ['src/app-sdk/index.ts'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // PROTOCOL VALUES ONLY: the server's own action names, provider merge-state enums,
  // and the literals a user must TYPE to arm an irreversible action. Every string in
  // that module is compared by value against something outside the dashboard, so
  // translating one breaks the comparison — and for a confirmation token it makes the
  // action impossible to complete in nine languages.
  //
  // Scoped to this one file for exactly the reason `commitProfiler.tsx` and
  // `styles.ts` are: a shape rule cannot express "machine values, but only in this
  // module". Admitting them by shape instead (a snake_case / lowercase-prose
  // exclusion) was measured and rejected — it dropped 35 strings across 5 unrelated
  // files (`api/client.ts` 33 -> 29, `ChatInput.tsx` 23 -> 20, `TrustDropdown.tsx`
  // 2 -> 0, ...), the same "hands back unrelated files' debt" failure the
  // `object-properties: next` exclusion above refuses. See the module's own header.
  {
    files: ['src/apps/issue-radar/lib/wireValues.ts'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // PROTOCOL KEY NAMES ONLY, same category as `wireValues.ts` above: this module's
  // entire contents are the two spellings of kiro-cli's reserved tool-purpose
  // ARGUMENT NAME (`__tool_use_purpose` and the camelCased echo) plus the regex that
  // recognizes paraphrases of it. Each is compared by value against a key that
  // arrives on the wire, so translating one silently stops matching and the tool
  // pill falls back to raw command text in that locale — the exact defect the module
  // exists to fix.
  //
  // The module has no path to user-visible copy of its own: it reads a string OUT of
  // a payload and returns it verbatim. That returned string is model-authored prose,
  // not interface copy, and is guarded at RENDER time against the active UI language
  // by `utils/toolLabel.ts` instead.
  //
  // Scoped to this one file for the reason the three exemptions above are: a shape
  // rule cannot express "reserved argument names, but only in this module". The
  // dunder prefix looks like a self-anchoring shape, but admitting literals by it
  // would also release every `__proto__` / `__dirname` guard string elsewhere in the
  // tree from the gate.
  {
    files: ['src/utils/toolPurpose.ts'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // A GLSL-ONLY module: two shader programs (`VERT`, `FRAG`) as template literals,
  // plus CSS custom-property token names and Tailwind classes. The component
  // renders exactly one `<canvas aria-hidden="true">` and no text node, so it has
  // no path to user-visible copy at all.
  //
  // A `words.exclude` shape rule was tried first and cannot do this job: WebGL2
  // makes `#version 300 es` the mandatory first line, so `'#version [\\s\\S]*$'`
  // looks like a precise, self-anchoring shape — but upstream validates a template
  // literal QUASI BY QUASI (`no-literal-string.js` -> `TemplateLiteral`), and this
  // shader interpolates its loop bounds (`uColors[${MAX_COLORS}]`,
  // `i < ${MAX_STRANDS}`). Only the first chunk carries the version pragma; every
  // chunk after an interpolation starts mid-program, so the pattern exempts the
  // first and reports the second. Widening it to "C-like punctuation" would exempt
  // any prose carrying braces and semicolons.
  //
  // Scoped to this one file for the same reason as `styles.ts` above: a shape rule
  // cannot express "GLSL, but only in this module". Keep this module shader-only —
  // any copy later added here belongs in the catalog, not behind this exemption.
  {
    files: ['src/components/Strands.tsx'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // PROTOCOL VALUES ONLY, same category as `wireValues.ts` above: the two
  // Aperture-registered literals for the session-pulse survey (a radio
  // question's response values, and the question text itself). Both are
  // compared/sent by value against Aperture's registered form template
  // (category=KiroCrew, name=SessionFeedback, version=1.0.1) — ingestion  // brand-ok: registered category id
  // 400s on any text/type mismatch, so translating either would break the
  // submission rather than localize it. See the module's own header.
  {
    files: ['src/components/sessionPulseWireValues.ts'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },

  // SEARCH-KEYWORD SYNONYMS ONLY: a manual overlay of extra query terms merged
  // into the Settings search corpus so a query like "dark mode" finds a setting
  // whose label does not contain those words. Every value is a term matched
  // against the user's typed query, never rendered — translating one would break
  // the match in that locale while adding catalog noise for a word the user
  // typed in their own language anyway. The keys are setting ids (enforced by
  // settingsKeywords.test.ts). Scoped to this one file for the same reason as the
  // modules above: a shape rule cannot express "search synonyms, but only here",
  // and any real copy later added elsewhere still belongs in the catalog.
  {
    files: ['src/components/commandPalette/settingsKeywords.ts'],
    rules: {
      'i18next/no-literal-string': 'off',
    },
  },
]
