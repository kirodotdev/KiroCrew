/**
 * The find_ui index's source readers: what the dashboard's own source proves,
 * read with the TypeScript parser and never evaluated.
 *
 * The readers turn source text into plain data, with errors rather than a
 * throw where the source cannot prove something: whether a call is the
 * translate function (by its lexical binding, not its name), the label sources
 * an expression or element can render, the `uiLocation()` render-site markers
 * and the one label each registered control shows, literal lists and key maps
 * (tab lists, rail items), the route table and the `<NavItem>` paths, and the
 * interactive candidates the coverage report and the auto tier start from. A
 * new reader of dashboard source goes here, unless only the auto tier's page
 * map reads it (module edges and region uses live in `ui-index-auto.mjs`).
 *
 * `ui-index.mjs` re-exports the public names, and callers import them from
 * there. This module imports no other index module; `ui-index-auto.mjs`
 * imports its shared AST helpers and `ui-index.mjs` its `LITERAL_PREFIX`
 * (the list at the end).
 */

import ts from 'typescript'

import { collectFileScopeConsts, resolveStrings } from './i18n-key-resolve.mjs'

export const MARKER_HELPER = 'uiLocation'
export const MARKER_ATTR = 'data-ui-location'
/**
 * Components that draw one control per entry of their `segments` array: a
 * marker spread into ONE entry (`{ key, label, ...uiLocation('<id>') }`) names
 * that entry's control, and the entry's `label` is the label. Each forwards
 * the attribute to the control it draws, proven by a rendered test.
 */
export const SEGMENT_HOSTS = new Set(['SegmentedControl', 'CrewEditorNextRail', 'Tablist'])
const LITERAL_PREFIX = 'literal:'
/**
 * Catalog namespace of `label: { from: 'description' }` keys: static text that
 * says what a control with a runtime label is. Owned by find_ui alone, so no
 * on-screen copy can be quoted as one, and none of these is ever on screen.
 */
export const DESCRIPTION_PREFIX = 'uiLocations.description.'

/** Elements whose descendants are a separate control, not part of a label. */
const INTERACTIVE_TAGS = new Set([
  'button', 'a', 'input', 'select', 'textarea', 'summary',
  'Btn', 'SendBtn', 'Clickable', 'Link', 'NavLink', 'IconButton', 'MenuItem',
  'DropdownMenuItem', 'ContextMenuItem', 'Toggle', 'Switch', 'Checkbox',
])
const INTERACTIVE_ROLES = new Set(['button', 'link', 'menuitem', 'tab', 'checkbox', 'switch', 'option', 'radio'])

/** Parse one TSX/TS source with parent pointers (the resolver needs them). */
export function parseSource(text, fileName) {
  return ts.createSourceFile(
    fileName, text, ts.ScriptTarget.Latest, true,
    /\.tsx$/.test(fileName) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
}

function lineOf(sf, node) {
  return sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
}

function unwrapExpr(node) {
  let n = node
  while (n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n)
    || ts.isSatisfiesExpression?.(n))) n = n.expression
  return n
}

function tagName(el) {
  return el.tagName.getText()
}

function attrsOf(el) {
  return el.attributes.properties
}

function findAttr(el, name) {
  for (const a of attrsOf(el)) {
    if (ts.isJsxAttribute(a) && a.name.getText() === name) return a
  }
  return null
}

function staticAttrString(attr) {
  if (!attr?.initializer) return null
  if (ts.isStringLiteral(attr.initializer)) return attr.initializer.text
  if (ts.isJsxExpression(attr.initializer)) {
    const e = unwrapExpr(attr.initializer.expression)
    if (e && (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e))) return e.text
  }
  return null
}

function isInteractive(opening) {
  if (INTERACTIVE_TAGS.has(tagName(opening))) return true
  const role = staticAttrString(findAttr(opening, 'role'))
  if (role && INTERACTIVE_ROLES.has(role)) return true
  return Boolean(findAttr(opening, 'onClick'))
}

// ---------------------------------------------------------------- translator bindings

/** Every identifier a binding name declares (`a`, `{ a, b: c }`, `[a, ...b]`). */
function bindingNames(name, out = []) {
  if (ts.isIdentifier(name)) out.push(name)
  else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
    for (const el of name.elements) if (ts.isBindingElement(el)) bindingNames(el.name, out)
  }
  return out
}

/** The declaration in `scope` (one lexical scope, not its children) that binds `name`. */
function declarationIn(scope, name) {
  const hit = (decl) => bindingNames(decl.name).some((id) => id.text === name)
  const fromDeclList = (list) => {
    for (const d of list.declarations) {
      const id = bindingNames(d.name).find((x) => x.text === name)
      if (id) return id.parent
    }
    return null
  }
  if (ts.isFunctionLike(scope)) {
    for (const p of scope.parameters) {
      const id = bindingNames(p.name).find((x) => x.text === name)
      if (id) return id.parent
    }
    return null
  }
  if (ts.isCatchClause(scope)) {
    return scope.variableDeclaration && hit(scope.variableDeclaration) ? scope.variableDeclaration : null
  }
  if ((ts.isForStatement(scope) || ts.isForOfStatement(scope) || ts.isForInStatement(scope))
    && scope.initializer && ts.isVariableDeclarationList(scope.initializer)) {
    return fromDeclList(scope.initializer)
  }
  const statements = ts.isSourceFile(scope) || ts.isBlock(scope) || ts.isModuleBlock(scope)
    ? scope.statements
    : ts.isCaseClause(scope) || ts.isDefaultClause(scope) ? scope.statements : null
  if (!statements) return null
  for (const st of statements) {
    if (ts.isVariableStatement(st)) {
      const d = fromDeclList(st.declarationList)
      if (d) return d
    } else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name?.text === name) {
      return st
    } else if (ts.isImportDeclaration(st) && st.importClause) {
      const c = st.importClause
      if (c.name?.text === name) return c
      const nb = c.namedBindings
      if (nb && ts.isNamespaceImport(nb) && nb.name.text === name) return nb
      if (nb && ts.isNamedImports(nb)) {
        const spec = nb.elements.find((e) => e.name.text === name)
        if (spec) return spec
      }
    }
  }
  return null
}

/** The nearest declaration binding `ident` by lexical scope, or null when unbound. */
function lexicalBinding(ident) {
  for (let n = ident.parent; n; n = n.parent) {
    const d = declarationIn(n, ident.text)
    if (d) return d
  }
  return null
}

const I18N_T_MODULE_RE = /(^|\/)i18n\/t$|^\.\/t$/
const isUseTranslationCall = (init) => {
  const e = unwrapExpr(init)
  return Boolean(e && ts.isCallExpression(e) && /(^|\.)useTranslation$/.test(e.expression.getText()))
}

/**
 * Is this call the translate function, proven by the callee's own lexical
 * binding rather than by name? Accepted bindings: `i18nT` (or an alias of it)
 * imported from the dashboard's `i18n/t`; `t` (or an alias) imported from
 * `i18next`; `t` (or an alias) destructured from `useTranslation()`; and
 * `i18next.t` / `i18n.t` where that object is an import. A parameter or local
 * that shadows the name is not a translator, whatever it is called, and an
 * unbound name proves nothing: both refuse, so the label reads as dynamic.
 */
export function isBoundTranslateCall(node) {
  if (!ts.isCallExpression(node)) return false
  const callee = node.expression
  if (ts.isIdentifier(callee)) {
    const decl = lexicalBinding(callee)
    if (!decl) return false
    if (ts.isImportSpecifier(decl)) {
      const imported = (decl.propertyName ?? decl.name).text
      const mod = decl.parent.parent.parent.moduleSpecifier.text
      return (imported === 'i18nT' && I18N_T_MODULE_RE.test(mod)) || (imported === 't' && mod === 'i18next')
    }
    if (ts.isBindingElement(decl) && ts.isObjectBindingPattern(decl.parent)) {
      const prop = decl.propertyName ?? decl.name
      const varDecl = decl.parent.parent
      return ts.isIdentifier(prop) && prop.text === 't'
        && ts.isVariableDeclaration(varDecl) && Boolean(varDecl.initializer) && isUseTranslationCall(varDecl.initializer)
    }
    return false
  }
  if (ts.isPropertyAccessExpression(callee) && callee.name.text === 't' && ts.isIdentifier(callee.expression)
    && /^(i18next|i18n)$/.test(callee.expression.text)) {
    const decl = lexicalBinding(callee.expression)
    return Boolean(decl && (ts.isImportSpecifier(decl) || ts.isImportClause(decl) || ts.isNamespaceImport(decl)))
  }
  return false
}

// ---------------------------------------------------------------- label expressions

/**
 * The finite set of label sources an expression can render: catalog keys from a
 * translate call, or literal text. `null` means unknowable (a variable, a
 * template, a call that is not a translation), which a registered label refuses.
 */
function labelSourcesOfExpr(expr, ctx) {
  const e = unwrapExpr(expr)
  if (!e) return []
  if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) {
    return e.text.trim() ? [{ literal: e.text.trim() }] : []
  }
  if (isBoundTranslateCall(e)) {
    // A second argument may interpolate; the key itself must still be finite.
    const keys = e.arguments[0] ? resolveStrings(e.arguments[0], ctx.consts) : null
    return keys ? keys.map((key) => ({ key })) : null
  }
  if (ts.isConditionalExpression(e)) {
    const a = labelSourcesOfExpr(e.whenTrue, ctx)
    const b = labelSourcesOfExpr(e.whenFalse, ctx)
    return a && b ? [...a, ...b] : null
  }
  if (ts.isBinaryExpression(e)) {
    const op = e.operatorToken.kind
    if (op === ts.SyntaxKind.QuestionQuestionToken || op === ts.SyntaxKind.BarBarToken) {
      const a = labelSourcesOfExpr(e.left, ctx)
      const b = labelSourcesOfExpr(e.right, ctx)
      return a && b ? [...a, ...b] : null
    }
  }
  return null
}

/**
 * Visible text of an element, excluding nested interactive children.
 *
 * Returns `{ sources, excluded, dynamic }`: `excluded` names nested controls
 * that were skipped (the Clear button inside Older Sessions), `dynamic` the
 * lines whose text could not be resolved.
 */
function collectText(node, ctx, out) {
  const visitChildren = (children) => {
    for (const child of children) visit(child)
  }
  const visitJsxBearing = (expr) => {
    const e = unwrapExpr(expr)
    if (!e) return
    if (ts.isJsxElement(e) || ts.isJsxSelfClosingElement(e) || ts.isJsxFragment(e)) {
      visit(e)
      return
    }
    if (e.kind === ts.SyntaxKind.NullKeyword || e.kind === ts.SyntaxKind.FalseKeyword) return
    if (ts.isBinaryExpression(e) && e.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) {
      // `cond && <X/>`: the left side is a condition, not rendered text.
      visitJsxBearing(e.right)
      return
    }
    if (ts.isConditionalExpression(e)) {
      visitJsxBearing(e.whenTrue)
      visitJsxBearing(e.whenFalse)
      return
    }
    const sources = labelSourcesOfExpr(e, ctx)
    if (sources) out.sources.push(...sources)
    else out.dynamic.push({ line: lineOf(ctx.sf, e), text: e.getText(ctx.sf).slice(0, 80) })
  }
  const visit = (n) => {
    if (ts.isJsxText(n)) {
      const text = n.getText(ctx.sf).replace(/\s+/g, ' ').trim()
      if (text) out.sources.push({ literal: text })
    } else if (ts.isJsxExpression(n)) {
      if (n.expression) visitJsxBearing(n.expression)
    } else if (ts.isJsxElement(n)) {
      if (n !== node && isInteractive(n.openingElement)) {
        out.excluded.push({ tag: tagName(n.openingElement), line: lineOf(ctx.sf, n) })
        return
      }
      visitChildren(n.children)
    } else if (ts.isJsxSelfClosingElement(n)) {
      if (n !== node && isInteractive(n)) out.excluded.push({ tag: tagName(n), line: lineOf(ctx.sf, n) })
    } else if (ts.isJsxFragment(n)) {
      visitChildren(n.children)
    }
  }
  visit(node)
  return out
}

function dedupeSources(sources) {
  const seen = new Set()
  const out = []
  for (const s of sources) {
    const id = s.key ?? `${LITERAL_PREFIX}${s.literal}`
    if (seen.has(id)) continue
    seen.add(id)
    out.push(s)
  }
  return out
}

// ---------------------------------------------------------------- markers

/**
 * The `<Host segments={[…]}>` a marker spread into one segment entry belongs
 * to (see {@link SEGMENT_HOSTS}), or null. Only that exact shape: the spread is
 * a property of an object literal that is an element of the array literal
 * passed straight to the host's `segments` attribute.
 */
function segmentHostOf(call) {
  const spread = call.parent
  if (!spread || !ts.isSpreadAssignment(spread)) return null
  const entry = spread.parent
  if (!entry || !ts.isObjectLiteralExpression(entry)) return null
  const list = entry.parent
  if (!list || !ts.isArrayLiteralExpression(list)) return null
  const expr = list.parent
  const attr = expr && ts.isJsxExpression(expr) ? expr.parent : null
  if (!attr || !ts.isJsxAttribute(attr) || attr.name.getText() !== 'segments') return null
  const opening = attr.parent?.parent
  if (!opening || !(ts.isJsxOpeningElement(opening) || ts.isJsxSelfClosingElement(opening))) return null
  if (!SEGMENT_HOSTS.has(tagName(opening))) return null
  return { opening, element: ts.isJsxOpeningElement(opening) ? opening.parent : opening, entry }
}

/**
 * Every `uiLocation('<id>')` spread and `data-ui-location="<id>"` attribute in
 * one source, with the element it sits on.
 */
export function scanMarkerSource(text, absPath, relPath) {
  const sites = []
  const errors = []
  if (!text.includes(MARKER_HELPER) && !text.includes(MARKER_ATTR) && ![...REF_SPREAD_HELPERS].some(h => text.includes(h))) return { sites, errors }
  const sf = parseSource(text, absPath)
  const ctx = { sf, consts: collectFileScopeConsts(sf, absPath) }
  const claimed = new Set()

  const record = (opening, element, idExpr, at, segment) => {
    const e = unwrapExpr(idExpr)
    if (!e || !(ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e))) {
      errors.push(`${relPath}:${lineOf(sf, at)}: ui location marker id must be a string literal`)
      return
    }
    const site = { id: e.text, rel: relPath, line: lineOf(sf, at), opening, element, ctx }
    if (segment) site.segment = segment
    sites.push(site)
  }

  const visit = (node) => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      // Each registering helper's spread carries its own `ref`; two on one
      // element (or one beside an explicit `ref`) leave only the last, so an
      // identity silently goes unregistered. Pass one helper's ref to the
      // other as its `own` argument instead.
      const refSpreads = attrsOf(node).filter(a => ts.isJsxSpreadAttribute(a) && spreadRegisters(a.expression))
      if (refSpreads.length > 1) {
        errors.push(`${relPath}:${lineOf(sf, refSpreads[1])}: two registering spreads on one element keep only one ref; pass one helper's ref to the other (e.g. ${MARKER_HELPER}(id, guideAnchor(name).ref)) and spread one`)
      }
      if (refSpreads.length > 0) {
        const explicit = attrsOf(node).find(b => ts.isJsxAttribute(b) && b.name.getText() === 'ref')
        const viaMarker = refSpreads.some(a => { const c = unwrapExpr(a.expression); return c && ts.isCallExpression(c) && ts.isIdentifier(c.expression) && c.expression.text === MARKER_HELPER })
        if (explicit && !viaMarker) errors.push(`${relPath}:${lineOf(sf, explicit)}: pass this element's ref to the registering helper as its own ref, not as a ref attribute beside the spread`)
      }
      const element = ts.isJsxOpeningElement(node) ? node.parent : node
      for (const a of attrsOf(node)) {
        if (ts.isJsxSpreadAttribute(a)) {
          const call = unwrapExpr(a.expression)
          if (call && ts.isCallExpression(call) && ts.isIdentifier(call.expression)
            && call.expression.text === MARKER_HELPER) {
            claimed.add(call)
            if (call.arguments.length < 1 || call.arguments.length > 2) {
              errors.push(`${relPath}:${lineOf(sf, a)}: ${MARKER_HELPER}() takes one id and, optionally, the element's own ref`)
            } else {
              // The spread carries the registering ref: an explicit `ref` on
              // the same element would replace it (or be replaced by it), so
              // the element's own ref goes in as the helper's second argument.
              const ownRef = attrsOf(node).find(b => ts.isJsxAttribute(b) && b.name.getText() === 'ref')
              if (ownRef) errors.push(`${relPath}:${lineOf(sf, ownRef)}: pass this element's ref as ${MARKER_HELPER}(id, ref), not as a ref attribute beside the spread`)
              record(node, element, call.arguments[0], a)
            }
          }
        } else if (ts.isJsxAttribute(a) && a.name.getText() === MARKER_ATTR) {
          // A bare attribute registers nothing (only the helper's ref does),
          // so the guide could never resolve the control it claims to mark.
          errors.push(`${relPath}:${lineOf(sf, a)}: spread {...${MARKER_HELPER}(id)} instead of a bare ${MARKER_ATTR} attribute; only the helper registers the element`)
        }
      }
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === MARKER_HELPER
      && !claimed.has(node) && !ts.isFunctionDeclaration(node.parent)) {
      const host = segmentHostOf(node)
      if (host) {
        claimed.add(node)
        if (node.arguments.length !== 1) {
          errors.push(`${relPath}:${lineOf(sf, node)}: ${MARKER_HELPER}() in a segment takes exactly one id`)
        } else record(host.opening, host.element, node.arguments[0], node, host.entry)
        ts.forEachChild(node, visit)
        return
      }
      // Only a JSX spread proves which element carries the id; a marker built
      // elsewhere and passed around cannot be tied to a render site (a segment
      // of a SEGMENT_HOSTS component is the one exception, above).
      const inSpread = ts.isJsxSpreadAttribute(node.parent)
        || (ts.isParenthesizedExpression(node.parent) && ts.isJsxSpreadAttribute(node.parent.parent))
      if (!inSpread) errors.push(`${relPath}:${lineOf(sf, node)}: ${MARKER_HELPER}() must be spread directly onto a JSX element`)
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sf, visit)
  return { sites, errors }
}

/** Helpers whose returned props carry a registering `ref` (`src/uiLocations/targetRegistry.ts`). */
const REF_SPREAD_HELPERS = new Set([
  MARKER_HELPER, 'forwardUiLocation', 'guideTarget', 'guideAnchor', 'guidePick', 'guidePickAlias',
  'guidePickOf', 'guidePickControl', 'guideConfirm',
])

/** Whether a spread expression can hand the element a registering helper's props (directly, or one branch of a conditional or `maybe`). */
function spreadRegisters(expr) {
  let found = false
  const walk = (n) => {
    if (found) return
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && REF_SPREAD_HELPERS.has(n.expression.text)) { found = true; return }
    // A function passed along (maybe(v, guidePick)) registers too.
    if (ts.isIdentifier(n) && REF_SPREAD_HELPERS.has(n.text) && n.parent && ts.isCallExpression(n.parent) && n.parent.arguments.includes(n)) { found = true; return }
    // A nested helper's own ref passed as an argument is not a second spread.
    if (ts.isPropertyAccessExpression(n) && n.name.text === 'ref') return
    ts.forEachChild(n, walk)
  }
  walk(expr)
  return found
}

/**
 * The one label key (or literal) a registered control renders, per its
 * descriptor's `label` rule. Errors when the site shows no label, an
 * unresolvable one, or several without the descriptor naming which.
 */
export function resolveSiteLabel(site, descriptor) {
  const rule = descriptor.label ?? { from: 'text' }
  const where = `${site.rel}:${site.line}`
  let sources
  let excluded = []
  if (site.segment) {
    // A segment's control shows its entry's `label` (see SEGMENT_HOSTS).
    if (rule.from !== 'text') return { error: `${where}: '${site.id}' is a segment; its label is the entry's label, so label.from must be 'text'` }
    const prop = site.segment.properties.find((p) => ts.isPropertyAssignment(p) && p.name?.getText(site.ctx.sf) === 'label')
    sources = prop ? labelSourcesOfExpr(prop.initializer, site.ctx) : null
    if (!sources) return { error: `${where}: '${site.id}' segment label is missing or dynamic; a registered label must be a catalog key or literal` }
  } else if (rule.from === 'attr') {
    const attr = findAttr(site.opening, rule.attr)
    if (!attr) return { error: `${where}: '${site.id}' has no ${rule.attr} attribute to take its label from` }
    const lit = staticAttrString(attr)
    if (lit !== null) sources = [{ literal: lit }]
    else {
      const init = attr.initializer
      sources = init && ts.isJsxExpression(init) ? labelSourcesOfExpr(init.expression, site.ctx) : null
    }
    if (!sources) return { error: `${where}: '${site.id}' ${rule.attr} is dynamic; a registered label must be a catalog key or literal` }
  } else if (rule.from === 'text') {
    const out = collectText(site.element, site.ctx, { sources: [], excluded: [], dynamic: [] })
    if (out.dynamic.length > 0) {
      return { error: `${where}: '${site.id}' label text is dynamic (${out.dynamic.map((d) => `line ${d.line}: ${d.text}`).join('; ')})` }
    }
    sources = out.sources
    excluded = out.excluded
  } else if (rule.from === 'description') {
    // The visible label is runtime data (a model name, a mode): the index
    // carries a static description instead, never quoted as on-screen text.
    // Allowed only where the site really has no static label to read.
    const underlying = rule.attr !== undefined ? { from: 'attr', attr: rule.attr } : { from: 'text' }
    const probe = resolveSiteLabel(site, { label: underlying })
    if (probe.source) {
      const shown = probe.source.key ?? JSON.stringify(probe.source.literal)
      return { error: `${where}: '${site.id}' renders a static label (${shown}); use label.from '${underlying.from}', not a description` }
    }
    if (!/dynamic/.test(probe.error ?? '')) return probe
    if (typeof rule.key !== 'string' || !rule.key.startsWith(DESCRIPTION_PREFIX)) {
      return { error: `${where}: '${site.id}' description key must be a catalog key under '${DESCRIPTION_PREFIX}' (owned by find_ui, never on-screen copy)` }
    }
    return { source: { key: rule.key }, excluded: [], description: true }
  } else {
    return { error: `${where}: '${site.id}' has an unknown label rule '${rule.from}'` }
  }
  sources = dedupeSources(sources)
  if (sources.length === 0) return { error: `${where}: '${site.id}' renders no label text` }
  if (rule.key !== undefined) {
    const hit = sources.find((s) => s.key === rule.key)
    if (!hit) {
      return { error: `${where}: '${site.id}' label.key '${rule.key}' is not what the site renders (${sources.map((s) => s.key ?? JSON.stringify(s.literal)).join(', ')})` }
    }
    return { source: hit, excluded }
  }
  if (sources.length > 1) {
    return { error: `${where}: '${site.id}' renders several labels (${sources.map((s) => s.key ?? JSON.stringify(s.literal)).join(', ')}); name one with label.key` }
  }
  return { source: sources[0], excluded }
}

// ---------------------------------------------------------------- bounded list adapters

function findContainer(sf, name) {
  let found = null
  const visit = (node) => {
    if (found) return
    if ((ts.isVariableDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name?.getText(sf) === name) {
      found = node
      return
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sf, visit)
  return found
}

/**
 * `{ <idProp>: '<literal>', <labelProp>: <label> }` objects inside one named
 * declaration (a tab list or a rail item array): the bounded adapter for a
 * literal list the page renders, read without evaluating the module.
 */
export function collectObjectList(text, absPath, relPath, { container, idProp = 'key', labelProp = 'label' }) {
  const sf = parseSource(text, absPath)
  const ctx = { sf, consts: collectFileScopeConsts(sf, absPath) }
  const root = findContainer(sf, container)
  if (!root) return { items: [], errors: [`${relPath}: no declaration named '${container}'`] }
  const items = []
  const errors = []
  const visit = (node) => {
    if (ts.isObjectLiteralExpression(node)) {
      let id = null
      let labelInit = null
      for (const p of node.properties) {
        if (!ts.isPropertyAssignment(p) || !p.name) continue
        const name = p.name.getText(sf)
        if (name === idProp) {
          const v = unwrapExpr(p.initializer)
          if (v && ts.isStringLiteral(v)) id = v.text
        } else if (name === labelProp) labelInit = p.initializer
      }
      if (id !== null && labelInit) {
        const sources = labelSourcesOfExpr(labelInit, ctx)
        if (!sources || sources.length !== 1) {
          errors.push(`${relPath}:${lineOf(sf, node)}: ${container} entry '${id}' has no single static label`)
        } else items.push({ id, ...sources[0], line: lineOf(sf, node) })
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(root)
  if (items.length === 0) errors.push(`${relPath}: '${container}' yielded no entries`)
  return { items, errors }
}

/** `export const MAP = { id: 'catalog.key', … }`: a key map read as data. */
export function collectKeyMap(text, absPath, relPath, constName) {
  const sf = parseSource(text, absPath)
  const root = findContainer(sf, constName)
  const init = root && ts.isVariableDeclaration(root) ? unwrapExpr(root.initializer) : null
  if (!init || !ts.isObjectLiteralExpression(init)) return { items: [], errors: [`${relPath}: '${constName}' is not an object literal`] }
  const items = []
  const errors = []
  for (const p of init.properties) {
    const v = ts.isPropertyAssignment(p) ? unwrapExpr(p.initializer) : null
    if (!v || !ts.isStringLiteral(v)) {
      errors.push(`${relPath}:${lineOf(sf, p)}: '${constName}' entry is not a string literal`)
      continue
    }
    items.push({ id: p.name.getText(sf).replace(/^['"]|['"]$/g, ''), key: v.text })
  }
  return { items, errors }
}

// ---------------------------------------------------------------- route table

/** The leading literal of a redirect target: `'/x'`, `'/x' + search`, `` `/x${…}` ``. */
function leadingLiteral(expr) {
  const e = unwrapExpr(expr)
  if (!e) return null
  if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return e.text
  if (ts.isTemplateExpression(e)) return e.head.text || null
  if (ts.isBinaryExpression(e) && e.operatorToken.kind === ts.SyntaxKind.PlusToken) return leadingLiteral(e.left)
  return null
}

function jsxOpening(node) {
  const e = unwrapExpr(node)
  if (!e) return null
  if (ts.isJsxSelfClosingElement(e)) return e
  if (ts.isJsxElement(e)) return e.openingElement
  return null
}

/** `to` of the first `<Navigate>` inside `node`, as `{ target }` or `{ error }`; null when none. */
function navigateTarget(node, sf) {
  let found = null
  const visit = (n) => {
    if (found) return
    const op = ts.isJsxSelfClosingElement(n) ? n : ts.isJsxOpeningElement(n) ? n : null
    if (op && tagName(op) === 'Navigate') {
      const to = findAttr(op, 'to')
      const lit = to?.initializer && ts.isStringLiteral(to.initializer) ? to.initializer.text
        : to?.initializer && ts.isJsxExpression(to.initializer) ? leadingLiteral(to.initializer.expression) : null
      found = lit && lit.startsWith('/') ? { target: lit } : { error: `line ${lineOf(sf, op)}: <Navigate to> is not a static path` }
      return
    }
    ts.forEachChild(n, visit)
  }
  visit(node)
  return found
}

/**
 * Every `<Route path="…" element={…}>` in the dashboard's route table, read
 * without evaluating App.tsx: its path pattern, whether it is a catch-all
 * (`*`, or a first segment that is a parameter), and the static target when
 * its element only redirects (`<Navigate to>` directly, or a same-file
 * component whose body renders one). A redirect whose target is not a static
 * path is an error: the index cannot say where it lands.
 */
export function collectRouteTable(text, absPath, relPath, extraSources = []) {
  const sf = parseSource(text, absPath)
  const components = new Map()
  // Redirect components the route table imports from another module (the
  // shell's `routes.tsx`) are read from that module's own source.
  const sources = [sf, ...extraSources.map(({ text: t, absPath: a }) => parseSource(t, a))]
  for (const st of sources.flatMap((s) => [...s.statements])) {
    if (ts.isFunctionDeclaration(st) && st.name) components.set(st.name.text, st)
    if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (ts.isIdentifier(d.name) && d.initializer && (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))) {
          components.set(d.name.text, d.initializer)
        }
      }
    }
  }
  const routes = []
  const errors = []
  const visit = (node) => {
    const op = ts.isJsxSelfClosingElement(node) ? node : ts.isJsxElement(node) ? node.openingElement : null
    if (op && tagName(op) === 'Route') {
      const routePath = staticAttrString(findAttr(op, 'path'))
      const el = findAttr(op, 'element')
      if (routePath !== null) {
        const elOpening = el?.initializer && ts.isJsxExpression(el.initializer) ? jsxOpening(el.initializer.expression) : null
        let redirect = null
        if (elOpening && tagName(elOpening) === 'Navigate') redirect = navigateTarget(elOpening, sf)
        else if (elOpening && components.has(tagName(elOpening))) {
          const body = components.get(tagName(elOpening))
          // A component that only redirects: its whole body renders one Navigate and nothing else.
          const hasOtherJsx = (() => {
            let other = false
            const v = (n) => {
              const o = ts.isJsxSelfClosingElement(n) ? n : ts.isJsxOpeningElement(n) ? n : null
              if (o && tagName(o) !== 'Navigate') other = true
              if (!other) ts.forEachChild(n, v)
            }
            v(body)
            return other
          })()
          if (!hasOtherJsx) redirect = navigateTarget(body, sf)
        }
        const first = routePath.split('/').filter(Boolean)[0] ?? ''
        const catchAll = routePath === '*' || first.startsWith(':')
        // A catch-all is never an answer, so where it sends is not checked.
        if (redirect?.error && !catchAll) errors.push(`${relPath}: route '${routePath}' ${redirect.error}`)
        routes.push({
          path: routePath,
          line: lineOf(sf, op),
          catchAll,
          redirect: redirect?.target ?? null,
        })
      }
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sf, visit)
  if (routes.length === 0) errors.push(`${relPath}: no <Route path> entries found`)
  return { routes, errors }
}

/**
 * The static `path` of every `<NavItem>` the shell draws (App.tsx): the pages
 * the navigation rail has an entry for, besides the registry's own rows
 * (Discover and Library are drawn by hand in the rail's Apps section). A page
 * whose route is here is a rail page: its guide points at that entry.
 */
export function collectNavItemPaths(text, absPath) {
  const sf = parseSource(text, absPath)
  const paths = new Set()
  const visit = (node) => {
    const op = ts.isJsxSelfClosingElement(node) ? node : ts.isJsxElement(node) ? node.openingElement : null
    if (op && tagName(op) === 'NavItem') {
      const p = staticAttrString(findAttr(op, 'path'))
      if (p && p.startsWith('/')) paths.add(p)
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sf, visit)
  return paths
}

/** How many leading segments of `pathname` a route pattern matches statically, or -1. */
function patternScore(pattern, pathname) {
  const pat = pattern.split('/').filter(Boolean)
  const segs = pathname.split('/').filter(Boolean)
  let statics = 0
  for (let i = 0; i < pat.length; i++) {
    const p = pat[i]
    if (p === '*') return statics
    const s = segs[i]
    if (p.startsWith(':')) {
      if (s === undefined && !p.endsWith('?')) return -1
      continue
    }
    if (s !== p) return -1
    statics++
  }
  return segs.length > pat.length ? -1 : statics
}

/** The route-table entry a URL lands on, most static segments first; catch-alls never count. */
export function matchRoute(routes, url) {
  const pathname = url.split('?')[0].split('#')[0]
  let best = null
  let bestScore = -1
  for (const r of routes) {
    if (r.catchAll) continue
    const s = patternScore(r.path, pathname)
    if (s > bestScore) {
      best = r
      bestScore = s
    }
  }
  return best
}

// ---------------------------------------------------------------- candidates (coverage report)

/**
 * Interactive elements with a statically resolvable label that are not yet
 * registered: the menu the next coverage wave picks targets from. A report,
 * never a gate.
 */
/** Whether label sources are exactly one catalog key and nothing else. */
function oneStaticKey(sources) {
  return Boolean(sources && sources.length === 1 && sources[0].key)
}

/**
 * Whether an element may draw text of its own between its tags. Only markup
 * proven to be text-free counts as icon-only: elements, `cond && <X/>`, a
 * ternary of those, and a call handed an element (`outcomeIcon(done, <Copy/>)`,
 * which picks an icon). Any text, and any other expression (a variable, a
 * translation), may be text.
 */
function hasOwnText(node, ctx) {
  if (!ts.isJsxElement(node)) return false
  const textFree = (expr) => {
    const e = unwrapExpr(expr)
    if (!e || e.kind === ts.SyntaxKind.NullKeyword || e.kind === ts.SyntaxKind.FalseKeyword) return true
    if (ts.isJsxElement(e) || ts.isJsxFragment(e)) return !children(e.children)
    if (ts.isJsxSelfClosingElement(e)) return true
    if (ts.isBinaryExpression(e) && e.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) return textFree(e.right)
    if (ts.isConditionalExpression(e)) return textFree(e.whenTrue) && textFree(e.whenFalse)
    if (ts.isCallExpression(e)) {
      return e.arguments.some((a) => {
        const x = unwrapExpr(a)
        return x && (ts.isJsxElement(x) || ts.isJsxSelfClosingElement(x))
      }) && e.arguments.every((a) => !ts.isStringLiteralLike(unwrapExpr(a) ?? a))
    }
    return false
  }
  // Whether these children may carry text.
  const children = (list) => list.some((c) => {
    if (ts.isJsxText(c)) return c.getText(ctx.sf).trim() !== ''
    if (ts.isJsxExpression(c)) return !textFree(c.expression)
    if (ts.isJsxElement(c) || ts.isJsxFragment(c)) return children(c.children)
    return false
  })
  return children(node.children)
}

/**
 * Whether a candidate is drawn as a destructive control: a shared Btn with a
 * `danger` prop (any value but a literal `{false}`), or an IconButton whose
 * `variant` is `danger` or not a static string at all.
 */
function isDangerVariant(opening) {
  const danger = findAttr(opening, 'danger')
  if (danger) {
    const e = danger.initializer && ts.isJsxExpression(danger.initializer) ? unwrapExpr(danger.initializer.expression) : null
    if (!(e && e.kind === ts.SyntaxKind.FalseKeyword)) return true
  }
  const variant = findAttr(opening, 'variant')
  if (variant) {
    const v = staticAttrString(variant)
    if (v === null || v === 'danger' || v === 'destructive') return true
  }
  return false
}

/** Tags whose contents are rendered only while the container is open. */
const CLOSED_CONTAINER_RE = /(?:Content|Dialog|Modal|Sheet|Drawer|Popover|Menu|Collapse|Collapsible|Disclosure)$|^GuideRevealScope$|^(?:details|dialog)$/

/**
 * Whether a candidate sits inside a JSX container that renders its contents
 * only while open (a menu, popover, dialog, sheet, tab panel, disclosure):
 * pointing at it would need a reveal step the auto tier cannot plan.
 */
function inClosedContainer(node) {
  for (let p = node.parent; p; p = p.parent) {
    const opening = ts.isJsxElement(p) ? p.openingElement : null
    if (opening && CLOSED_CONTAINER_RE.test(tagName(opening))) return true
  }
  return false
}

export function scanCandidates(text, absPath, relPath, sf = parseSource(text, absPath)) {
  const ctx = { sf, consts: collectFileScopeConsts(sf, absPath) }
  const found = []
  const visit = (node) => {
    const opening = ts.isJsxElement(node) ? node.openingElement : ts.isJsxSelfClosingElement(node) ? node : null
    if (opening && isInteractive(opening)) {
      let marked = false
      for (const a of attrsOf(opening)) {
        if (ts.isJsxSpreadAttribute(a) && a.expression.getText(sf).startsWith(`${MARKER_HELPER}(`)) marked = true
        if (ts.isJsxAttribute(a) && a.name.getText() === MARKER_ATTR) marked = true
      }
      const attrSources = []
      for (const attrName of ['aria-label', 'title', 'label']) {
        const attr = findAttr(opening, attrName)
        if (!attr) continue
        const lit = staticAttrString(attr)
        attrSources.push(lit !== null ? [{ literal: lit }]
          : attr.initializer && ts.isJsxExpression(attr.initializer) ? labelSourcesOfExpr(attr.initializer.expression, ctx) : null)
      }
      let sources = attrSources.length ? attrSources[0] : null
      // An icon-only control (no text of its own) names itself through its
      // attributes alone. When the first one is dynamic or names several keys
      // (`aria-label={copyOutcome(copied, t('copy'))}`) but a later one is ONE
      // static key (`title={t('copy')}`), that key is its label: it is what a
      // person hovering the icon reads, in every state.
      if (attrSources.length > 1 && !oneStaticKey(sources) && !hasOwnText(node, ctx)) {
        const alt = attrSources.find(oneStaticKey)
        if (alt) sources = alt
      }
      if (!sources && ts.isJsxElement(node)) {
        const out = collectText(node, ctx, { sources: [], excluded: [], dynamic: [] })
        sources = out.dynamic.length ? null : dedupeSources(out.sources)
      }
      found.push({
        rel: relPath,
        line: lineOf(sf, node),
        pos: node.getStart(sf),
        // Where a build-time `data-ui-auto` marker would go (right after the
        // tag name) and where the opening tag ends: see autoStampManifest.
        tagEnd: opening.tagName.getEnd(),
        openEnd: opening.getEnd(),
        tag: tagName(opening),
        role: staticAttrString(findAttr(opening, 'role')),
        type: staticAttrString(findAttr(opening, 'type')),
        spread: attrsOf(opening).some((a) => ts.isJsxSpreadAttribute(a)),
        danger: isDangerVariant(opening),
        inContainer: inClosedContainer(node),
        marked,
        keys: sources ? sources.filter((s) => s.key).map((s) => s.key) : [],
        literals: sources ? sources.filter((s) => s.literal).map((s) => s.literal) : [],
        resolved: Boolean(sources && sources.length > 0),
      })
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sf, visit)
  return found
}

// Shared with ui-index-auto.mjs (the AST helpers) and ui-index.mjs
// (LITERAL_PREFIX), which does not re-export them.
export { findAttr, LITERAL_PREFIX, staticAttrString, tagName, unwrapExpr }
