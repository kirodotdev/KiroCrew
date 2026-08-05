/**
 * Local eslint plugin: `eslint-plugin-i18next`'s `no-literal-string`, minus ONE
 * upstream behaviour that silently exempted a whole class of user-visible copy.
 *
 * ## The hole
 *
 * `eslint-plugin-i18next@6` suppresses every literal in the subtree of an
 * ALL-CAPS variable declarator (`lib/rules/no-literal-string.js`):
 *
 * ```js
 * VariableDeclarator(node) {
 *   // allow statements like const A_B = "test"
 *   indicatorStack.push(isUpperCase(node.id.name));
 * },
 * ```
 *
 * with `isUpperCase = (str) => /^[A-Z_-]+$/.test(str)`. A single `true` anywhere
 * on that stack short-circuits `validateBeforeReport()`, so the rule never looks
 * inside. The intent is sound for `const API_URL = 'https://…'`, but the naming
 * convention in this dashboard is that a module-level table of UI copy is
 * ALL-CAPS too — `SESSION_FILTERS`, `SORT_OPTIONS`, `EFFORT_DISPLAY`,
 * `EFFORT_HELP`. Those tables held real, shipped, untranslated English and the
 * gate reported them as clean:
 *
 *   - `pages/ChatSidebar.tsx` — the session filter and sort menus rendered
 *     `Unread` / `In progress` / `Newest` / `Created (Newest)` in every locale.
 *   - `lib/effort.ts` — the reasoning-effort picker rendered `High` in every
 *     locale, under a translated `强度` heading.
 *
 * Neither file's copy ever entered `src/i18n/untranslated-baseline.json`, so
 * neither was ever counted, scheduled, or fixed. The suppression sits upstream of
 * every knob in `eslint.i18n.config.js` — `words`, `callees`,
 * `object-properties` and `jsx-attributes` are all consulted AFTER it — so it
 * cannot be narrowed by configuration. Hence a wrapper.
 *
 * ## What this wrapper does
 *
 * Exactly one thing: it makes the declarator name irrelevant. Every other
 * upstream behaviour, and every option in `eslint.i18n.config.js`, passes through
 * untouched — this is not a reimplementation, it delegates to the upstream
 * visitor.
 *
 * The mechanism is deliberate. `indicatorStack` is closure-private, so the value
 * cannot be overwritten from outside; but the handler derives it from
 * `node.id.name`, so calling the upstream handler with a clone whose declarator
 * name is not ALL-CAPS makes it push `false` instead. The handler MUST still be
 * invoked exactly once per declarator: `'VariableDeclarator:exit'` pops
 * unconditionally, so skipping the push would desynchronise the stack and
 * suppress or unsuppress unrelated nodes.
 *
 * Machine constants stay quiet because the content filters still apply — a
 * storage key (`'mc-session-sort'`), an endpoint, a lowercase enum value
 * (`'low'`) all match `words.exclude`. What this surfaces is capitalised prose,
 * which is what a label table holds.
 */

import i18nextPlugin from 'eslint-plugin-i18next'

const upstream = i18nextPlugin.rules?.['no-literal-string']

/** Upstream's own test, from `eslint-plugin-i18next/lib/helper/index.js`. */
function isUpperCaseName(name) {
  return typeof name === 'string' && /^[A-Z_-]+$/.test(name)
}

/**
 * A placeholder declarator name guaranteed NOT to match `isUpperCaseName`, so
 * upstream pushes `false` for this declarator.
 */
const NEUTRAL_NAME = 'i18nStrictNeutralName'

/**
 * Fail loudly on a dependency bump that reshapes the upstream rule, instead of
 * silently degrading to "no i18n coverage at all". `no-literal-string` without a
 * `VariableDeclarator` handler would mean the hole closed upstream (delete this
 * wrapper) or that the visitor moved (rework it) — either way a human decision.
 */
function assertUpstreamShape(visitor) {
  if (typeof visitor.VariableDeclarator !== 'function') {
    throw new Error(
      'eslint-plugin-i18next no longer exposes a VariableDeclarator handler on '
      + 'no-literal-string. The ALL-CAPS suppression this wrapper removes may be '
      + 'gone (delete website/eslint-rules/i18n-strict.js and re-enable '
      + 'i18next/no-literal-string) or may have moved (rework the wrapper). '
      + 'Do not ship a silently weaker i18n gate.',
    )
  }
  if (typeof visitor['VariableDeclarator:exit'] !== 'function') {
    throw new Error(
      'eslint-plugin-i18next no longer pairs VariableDeclarator with an :exit '
      + 'handler. This wrapper relies on push/pop symmetry; rework it before use.',
    )
  }
}

/**
 * Suffix appended to a finding that exists ONLY because this wrapper looked
 * inside an ALL-CAPS constant.
 *
 * One lint pass has to answer two questions: what the upstream rule would have
 * reported (the per-file ceilings in `untranslated-baseline.json`, which must not
 * be re-snapshotted) and what it hid (the strict-only debt). Tagging is what makes
 * one pass enough — `scripts/check-i18n-strings.mjs` splits on this marker instead
 * of running eslint over the tree twice.
 *
 * A suffix, not a prefix: `classify()` in that script anchors several of its
 * patterns at the start of the message.
 */
export const ALL_CAPS_MARKER = ' [i18n-strict:all-caps-const]'

/** @type {import('eslint').Rule.RuleModule} */
const noLiteralString = {
  meta: upstream?.meta,
  create(context) {
    // Depth, not a boolean: declarators nest, and a finding is upstream-suppressed
    // if ANY enclosing declarator is ALL-CAPS (`indicatorStack.some(…)`).
    let capsDepth = 0
    // Prototype shadowing, NOT a Proxy: eslint 9 defines `report` as a read-only,
    // non-configurable own property on the context, and a Proxy `get` trap that
    // returns anything else violates the invariant and throws
    // ("'get' on proxy: property 'report' is a read-only and non-configurable data
    // property"). An object whose prototype IS the context shadows `report`
    // legally and leaves every other lookup untouched.
    const tagging = Object.create(context, {
      report: {
        value: (descriptor) => (
          capsDepth === 0
            ? context.report(descriptor)
            : context.report({
              ...descriptor,
              message: `${descriptor.message ?? ''}${ALL_CAPS_MARKER}`,
            })
        ),
      },
    })

    const visitor = upstream.create(tagging)
    assertUpstreamShape(visitor)
    const enter = visitor.VariableDeclarator
    const exit = visitor['VariableDeclarator:exit']

    return {
      ...visitor,
      VariableDeclarator(node) {
        if (!isUpperCaseName(node.id?.name)) {
          enter(node)
          return
        }
        capsDepth += 1
        // Same node, non-ALL-CAPS name: upstream pushes `false`, and its stack
        // still balances against the `:exit` handler below.
        enter({ ...node, id: { ...node.id, name: NEUTRAL_NAME } })
      },
      'VariableDeclarator:exit'(node) {
        // Decrement BEFORE delegating, mirroring upstream's own ordering: its
        // `:exit` is a bare pop that reports nothing today, but a future version
        // that reported here would see a depth this handler had not yet closed.
        if (isUpperCaseName(node.id?.name)) capsDepth -= 1
        exit(node)
      },
    }
  },
}

export default {
  meta: { name: 'i18n-strict' },
  rules: { 'no-literal-string': noLiteralString },
}
