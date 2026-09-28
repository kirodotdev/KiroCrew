/**
 * Local eslint rule: the one-shot approval decision must come from the shared
 * mapping, never from a decision computed at the call site.
 *
 * ## The class this closes
 *
 * `POST /api/approvals/{id}/{action}` honors exactly `approve`, `reject` and
 * `reject_once`, and records NO standing grant. A trust verb (`trust`,
 * `trust_reads`, `trust_command`, `trust_base`) has no representation there and
 * belongs on a grant-recording endpoint. Three surfaces shipped the same defect
 * independently -- #5400 (spawn-approval card), #5434 (collapsed tool row),
 * #5486 (ChatInput) -- each by computing its own decision, which mapped a trust
 * verb onto `approve`. The tool ran once, the UI reported a standing grant, and
 * the backend recorded nothing; the user's next identical action prompted again,
 * which reads as the grant having been forgotten.
 *
 * ## Why a lint rule, and not a runtime guard
 *
 * Two chokepoints already exist and neither can fire:
 *
 *   - `api.resolveApproval` is typed to the three honored actions.
 *   - `api_approval_resolve` returns 400 for anything outside them.
 *
 * Both reject a trust verb SENT to the endpoint, but the verb never arrives as
 * itself. A call-site mapping converts it into `approve` BEFORE the request is
 * made, so by the time either check runs, the information that standing trust
 * was requested has been destroyed. A guard cannot recover what the caller
 * erased before calling. Source is the only layer where the verb still exists as
 * itself, which is why this is a lint rule.
 *
 * ## Why an ALLOWLIST and not a denylist of ternaries
 *
 * This rule first shipped as a denylist of two node types
 * (`ConditionalExpression`, `LogicalExpression`) and that was too narrow to back
 * the guarantee it was written for. Two re-introduction paths passed it in
 * silence:
 *
 *   - a module-private mapper -- `resolveApproval(id, myMap(action))` -- which
 *     is the shape ALL THREE cited defects actually took, so a ternary-only
 *     denylist would have caught none of them; and
 *   - the same ternary hoisted one line up into a local, which changes the node
 *     type at the argument to `Identifier` and nothing else.
 *
 * So the argument is checked against an allowlist instead, and an unrecognized
 * shape is REPORTED rather than admitted. That direction is deliberate: every
 * `resolveApproval` decision argument in the repo today is one of exactly three
 * shapes (a string literal, a plain identifier, or a `toApiDecision` call), so a
 * fourth shape is a thing to judge deliberately, not to wave through.
 *
 * ## What it does NOT catch
 *
 * Without type information the rule cannot tell a genuinely narrowed identifier
 * from a widened one, so a parameter declared `string` and passed straight
 * through still passes. What it does catch is a decision COMPUTED here: inline,
 * hoisted into a local, delegated to a private mapper, or delegated to a private
 * mapper that borrows the shared NAME. Closing the remaining gap needs a
 * type-aware lane (`parserOptions.project`), which is a separate change with its
 * own cost.
 */

/** The only actions the one-shot endpoint honors, so the only admissible literals. */
const HONORED_ACTIONS = new Set(['approve', 'reject', 'reject_once'])

/** The shared mapping every computed decision must come from. */
const SHARED_MAPPER = 'toApiDecision'

/** Expression types that COMPUTE a decision rather than deferring to the shared mapping. */
const COMPUTED_DECISION_TYPES = new Set(['ConditionalExpression', 'LogicalExpression'])

/** The module the shared mapping must actually come from. */
const SHARED_MODULE = 'utils/approvalDecision'

/**
 * True only for a call to the SHARED `toApiDecision` -- resolved by BINDING, not
 * by spelling.
 *
 * Checking the name alone was not enough, and the gap was the historical shape
 * exactly: #5486's defect was a module-private function named `toApiDecision`,
 * so a name-only test admitted the one thing this rule exists to stop. A local
 * function, a local `const`, or any other same-named declaration is therefore
 * reported; only an identifier whose binding is an IMPORT from the shared module
 * passes.
 *
 * A member call (`mod.toApiDecision(x)`) is not admitted either: `.toApiDecision`
 * has zero occurrences in the tree, so allowing it would widen the guard to any
 * object carrying a method by that name in exchange for no consumer.
 */
function isSharedMapperCall(node, scope, resolveVar) {
  const callee = node.callee
  if (callee.type !== 'Identifier' || callee.name !== SHARED_MAPPER) return false
  const variable = resolveVar(scope, callee.name)
  if (!variable) return false
  return variable.defs.some(def => {
    if (def.type !== 'ImportBinding') return false
    const source = def.parent && def.parent.source && def.parent.source.value
    return typeof source === 'string' && source.includes(SHARED_MODULE)
  })
}

/** Resolve an identifier to the variable it refers to, or null when it is not in scope. */
function resolveVariable(scope, name) {
  for (let s = scope; s; s = s.upper) {
    const found = s.variables.find(v => v.name === name)
    if (found) return found
  }
  return null
}

/**
 * Every expression a local identifier can carry: its declarator initializer plus
 * any later assignment. A `let` written from a ternary in a branch is the same
 * hoisted shape as a `const`, just spelled across two statements.
 */
function writtenExpressions(variable) {
  const out = []
  for (const def of variable.defs) {
    if (def.node && def.node.type === 'VariableDeclarator' && def.node.init) out.push(def.node.init)
  }
  for (const ref of variable.references) {
    if (ref.writeExpr) out.push(ref.writeExpr)
  }
  return out
}

/** @type {import('eslint').Rule.RuleModule} */
const noInlineOneShotDecision = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Require the one-shot approval decision to be a honored literal or a call to the shared toApiDecision mapping, never a decision computed at the call site.',
    },
    schema: [],
    messages: {
      computedDecision:
        "Do not compute the one-shot approval action here. Use the shared `toApiDecision` from utils/approvalDecision: a call-site mapping turns a trust verb into 'approve' before the request is made, so neither the typed client nor the backend's 400 can catch it (#5400, #5434, #5486).",
      foreignMapper:
        "Route the one-shot approval action through the shared `toApiDecision` from utils/approvalDecision, not `{{name}}`. A private mapper is exactly the shape #5400, #5434 and #5486 each shipped -- it type-checks and emits a legal action while silently upgrading a trust verb to 'approve'.",
      derivedDecision:
        "`{{name}}` is assigned a decision computed from a conditional here, which is the #5400/#5434/#5486 shape moved one line up. Assign it from the shared `toApiDecision` instead.",
      unknownLiteral:
        "'{{value}}' is not an action the one-shot endpoint honors (approve, reject, reject_once). A trust verb has no representation there -- use the slot-scoped endpoint to record a standing grant.",
      unknownShape:
        'Unrecognized one-shot approval decision ({{type}}). Pass a honored literal or the shared `toApiDecision` from utils/approvalDecision, or extend this rule deliberately if a new shape is genuinely needed.',
    },
  },
  create(context) {
    const sourceCode = context.sourceCode ?? context.getSourceCode()
    return {
      CallExpression(node) {
        const callee = node.callee
        const name =
          callee.type === 'MemberExpression' && !callee.computed && callee.property.type === 'Identifier'
            ? callee.property.name
            : callee.type === 'Identifier'
              ? callee.name
              : null
        if (name !== 'resolveApproval') return
        const decision = node.arguments[1]
        if (!decision) return
        const scope = sourceCode.getScope ? sourceCode.getScope(decision) : context.getScope()

        // A decision computed right here.
        if (COMPUTED_DECISION_TYPES.has(decision.type)) {
          context.report({ node: decision, messageId: 'computedDecision' })
          return
        }

        // A literal, which must name an action the endpoint actually honors.
        if (decision.type === 'Literal') {
          if (typeof decision.value === 'string' && !HONORED_ACTIONS.has(decision.value)) {
            context.report({ node: decision, messageId: 'unknownLiteral', data: { value: String(decision.value) } })
          }
          return
        }

        // A call, which must be the shared mapping and not a private one.
        if (decision.type === 'CallExpression') {
          if (!isSharedMapperCall(decision, scope, resolveVariable)) {
            context.report({
              node: decision,
              messageId: 'foreignMapper',
              data: { name: sourceCode.getText(decision.callee) },
            })
          }
          return
        }

        // An identifier: allowed when it relies on its DECLARED type (a
        // parameter, an import, a destructure), reported when it is assigned a
        // decision computed in this file.
        if (decision.type === 'Identifier') {
          const variable = resolveVariable(scope, decision.name)
          if (!variable) return
          for (const expr of writtenExpressions(variable)) {
            if (COMPUTED_DECISION_TYPES.has(expr.type)) {
              context.report({
                node: decision,
                messageId: 'derivedDecision',
                data: { name: decision.name },
              })
              return
            }
            if (expr.type === 'CallExpression' && !isSharedMapperCall(expr, scope, resolveVariable)) {
              context.report({
                node: decision,
                messageId: 'foreignMapper',
                data: { name: sourceCode.getText(expr.callee) },
              })
              return
            }
          }
          return
        }

        // Fail closed on a shape nobody enumerated.
        context.report({ node: decision, messageId: 'unknownShape', data: { type: decision.type } })
      },
    }
  },
}

export const rules = {
  'no-inline-one-shot-decision': noInlineOneShotDecision,
}

export default { rules }
