/**
 * The find_ui location index: pure building blocks for `gen-ui-index.mjs`.
 *
 * Nothing here walks the filesystem or exits the process; the entry script
 * gathers inputs and these functions turn them into one index plus a list of
 * errors. The fixture tests in `src/uiLocations/uiIndex.test.ts` drive the same
 * functions on synthetic sources.
 *
 * Three kinds of input become locations, and none of them is guessed:
 *
 *  - pages and tabs, from the registries the dashboard itself renders
 *    (`surfaceData.ts`, `pagesData.ts`, the Settings and Customize tab lists);
 *  - settings, projected from the committed settings extraction (the same
 *    entries settings search and `settings.show` use), joined by stable id;
 *  - registered controls: a `uiLocation('<id>')` marker at the real render site
 *    plus one descriptor in its area file, `src/uiLocations/areas/<area>.ts`
 *    (merged by {@link mergeLocationAreas}). The marker proves
 *    where the control is drawn and which catalog key its visible label comes
 *    from; the descriptor supplies what syntax cannot (parents, prerequisites).
 *
 * A registered control whose label, marker or parent cannot be proven is an
 * ERROR, never a best guess: a wrong path is worse than no path.
 */

import { createHash } from 'node:crypto'
import { posix as posixPath } from 'node:path'

import ts from 'typescript'

import { collectFileScopeConsts, resolveStrings } from './i18n-key-resolve.mjs'

export const SCHEMA_VERSION = 1
export const MARKER_HELPER = 'uiLocation'
export const MARKER_ATTR = 'data-ui-location'
/**
 * Components that draw one control per entry of their `segments` array: a
 * marker spread into ONE entry (`{ key, label, ...uiLocation('<id>') }`) names
 * that entry's control, and the entry's `label` is the label. Each forwards
 * the attribute to the control it draws, proven by a rendered test.
 */
export const SEGMENT_HOSTS = new Set(['SegmentedControl', 'CrewEditorRail'])

/** Prefixes owned by generated locations; a registered id may not use them. */
const RESERVED_PREFIXES = ['page.', 'tab.', 'settings.', 'setting:']
const LOCATION_ID_RE = /^[a-z0-9]+(?:[.-][a-z0-9]+)+$/
const LITERAL_PREFIX = 'literal:'
/**
 * Catalog namespace of `label: { from: 'description' }` keys: static text that
 * says what a control with a runtime label is. Owned by find_ui alone, so no
 * on-screen copy can be quoted as one, and none of these is ever on screen.
 */
export const DESCRIPTION_PREFIX = 'uiLocations.description.'
/** What the auto tier covers, said in the full index's scope and the auto artifact's. */
export const AUTO_SCOPE = 'auto-indexed controls (label and page proven from source, exact place and prerequisites unknown)'
/**
 * Name of the build-time auto-tier artifact. `npm run build` writes it into the
 * vite output (website/dist), so it ships wherever the dashboard bundle does
 * (src/kiro_crew/static/dist); it is never committed.
 */
export const AUTO_ARTIFACT_NAME = 'ui-index.auto.json'

const REQUIREMENT_KINDS = new Set(['shown_by', 'viewport', 'preview_flag', 'condition'])
const ENTRY_KINDS = new Set(['rail', 'tab', 'sidebar', 'menu', 'toolbar', 'header', 'content', 'direct-link'])
/**
 * The dashboard shell (App.tsx's top bar): drawn over every page, so a
 * placement on it has no parent and no route. Emitted as `surface_id: 'shell'`
 * with `route: ''` and an empty path; find_ui reports it as on every page.
 */
export const SHELL_SURFACE = 'shell'
const KINDS = new Set(['page', 'tab', 'setting', 'button', 'toggle', 'disclosure', 'menu-item', 'link', 'field', 'list'])

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

// ---------------------------------------------------------------- settings freshness

/**
 * Compare the live settings extraction (run in memory by the entry script)
 * with the two committed outputs this index is built from. Any byte difference
 * means the index would be generated from stale Settings controls.
 */
export function checkSettingsExtraction({ liveRegistrySource, liveAgentJson, committedRegistrySource, committedAgentJson }) {
  const errors = []
  if (liveRegistrySource !== committedRegistrySource) {
    errors.push('settingsRegistry.gen.ts does not match the live settings extraction; run npm run gen:settings')
  }
  if (liveAgentJson !== committedAgentJson) {
    errors.push('settings-registry.generated.json does not match the live settings extraction; run npm run gen:settings')
  }
  return errors
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

// ---------------------------------------------------------------- auto tier: the one page a file is drawn on

/**
 * The module edges of one source: static imports, re-exports, side-effect
 * imports and literal dynamic `import()` calls. A type-only import renders
 * nothing and is dropped. `binds` lists the local names an edge introduces
 * (`const X = lazy(() => import('./x'))` binds X), so a host file can say
 * which of its regions uses the module; an edge with no binding is used
 * wherever the file is.
 */
export function scanImports(sf) {
  const edges = []
  const bound = new Set()
  const literalImport = (n) => n.expression.kind === ts.SyntaxKind.ImportKeyword && n.arguments[0]
    && (ts.isStringLiteral(n.arguments[0]) || ts.isNoSubstitutionTemplateLiteral(n.arguments[0]))
  const importsIn = (root) => {
    const out = []
    const v = (n) => {
      if (ts.isCallExpression(n) && literalImport(n)) out.push(n)
      ts.forEachChild(n, v)
    }
    v(root)
    return out
  }
  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      const c = node.importClause
      if (c?.isTypeOnly) return
      const binds = []
      // Local name -> the name the module exports it under (`default` for a
      // default import), so `{ Btn as B }` is never taken for the shared Btn.
      const imported = {}
      if (c?.name) {
        binds.push(c.name.text)
        imported[c.name.text] = 'default'
      }
      const nb = c?.namedBindings
      if (nb && ts.isNamespaceImport(nb)) binds.push(nb.name.text)
      if (nb && ts.isNamedImports(nb)) {
        for (const e of nb.elements) {
          if (e.isTypeOnly) continue
          binds.push(e.name.text)
          imported[e.name.text] = (e.propertyName ?? e.name).text
        }
      }
      if (c && binds.length === 0) return // `import { type A }`: nothing that renders
      edges.push({ spec: node.moduleSpecifier.text, binds, imported })
      return
    }
    if (ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier) && !node.isTypeOnly) {
        edges.push({ spec: node.moduleSpecifier.text, binds: [] })
      }
      return
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const calls = importsIn(node.initializer)
      if (calls.length === 1) {
        bound.add(calls[0])
        edges.push({ spec: calls[0].arguments[0].text, binds: [node.name.text] })
      }
    }
    if (ts.isCallExpression(node) && literalImport(node) && !bound.has(node)) {
      edges.push({ spec: node.arguments[0].text, binds: [] })
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return edges
}

function isDeclarationOrNonUse(id) {
  const p = id.parent
  if (!p) return true
  if (ts.isImportSpecifier(p) || ts.isImportClause(p) || ts.isNamespaceImport(p)) return true
  if ((ts.isVariableDeclaration(p) || ts.isFunctionDeclaration(p) || ts.isClassDeclaration(p)
    || ts.isParameter(p) || ts.isBindingElement(p)) && p.name === id) return true
  if (ts.isBindingElement(p) && p.propertyName === id) return true
  if (ts.isPropertyAccessExpression(p) && p.name === id) return true
  if ((ts.isPropertyAssignment(p) || ts.isMethodDeclaration(p) || ts.isPropertyDeclaration(p)) && p.name === id) return true
  if (ts.isJsxAttribute(p) || ts.isJsxClosingElement(p)) return true
  if (ts.isQualifiedName(p) || ts.isTypeReferenceNode(p) || ts.isTypeQueryNode(p) || ts.isExpressionWithTypeArguments(p)) return true
  return false
}

/**
 * Which regions of a host file use each of `names`. `regions` are
 * `{ label, start, end }` spans (one Route's element, one tab's panel); a use
 * outside every span is `'*'`. Declarations, closing tags, property names and
 * type positions are not uses. A name never used maps to an empty set.
 */
export function classifyUses(sf, names, regions) {
  const uses = new Map([...names].map((n) => [n, new Set()]))
  const visit = (node) => {
    if (ts.isIdentifier(node) && uses.has(node.text) && !isDeclarationOrNonUse(node)) {
      const pos = node.getStart(sf)
      const hit = regions.filter((r) => pos >= r.start && pos < r.end)
      if (hit.length === 0) uses.get(node.text).add('*')
      for (const r of hit) uses.get(node.text).add(r.label)
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return uses
}

/** Every `<Route path="…" element={…}>`'s element span, labelled `route:<path>`. */
export function collectRouteElementSpans(sf) {
  const spans = []
  const visit = (node) => {
    const op = ts.isJsxSelfClosingElement(node) ? node : ts.isJsxElement(node) ? node.openingElement : null
    if (op && tagName(op) === 'Route') {
      const p = staticAttrString(findAttr(op, 'path'))
      const el = findAttr(op, 'element')
      if (p !== null && el?.initializer) spans.push({ label: `route:${p}`, path: p, start: el.initializer.getStart(sf), end: el.initializer.end })
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return spans
}

/**
 * The tab panels of a side-panel page, proven from its own source: inside a
 * render function passed as the child of an element that takes `tabs`
 * (`<SidePanelLayout tabs={…}>{tab => …}</SidePanelLayout>`), each
 * `tab === '<key>' && <Panel/>` whose key is one of `tabKeys`. Returns the
 * right-hand side spans labelled `tab:<key>`; nothing else counts as a panel.
 */
export function collectTabPanels(sf, tabKeys) {
  const spans = []
  const visitHost = (node) => {
    if (ts.isJsxElement(node) && findAttr(node.openingElement, 'tabs')) {
      for (const child of node.children) {
        const fn = ts.isJsxExpression(child) ? unwrapExpr(child.expression) : null
        if (!fn || !(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) continue
        const param = fn.parameters[0]?.name
        if (!param || !ts.isIdentifier(param)) continue
        const visitBody = (n) => {
          if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) {
            const cmp = unwrapExpr(n.left)
            if (cmp && ts.isBinaryExpression(cmp) && cmp.operatorToken.kind === ts.SyntaxKind.EqualsEqualsEqualsToken) {
              const [a, b] = [unwrapExpr(cmp.left), unwrapExpr(cmp.right)]
              const lit = ts.isIdentifier(a) && a.text === param.text && ts.isStringLiteral(b) ? b.text
                : ts.isIdentifier(b) && b.text === param.text && ts.isStringLiteral(a) ? a.text : null
              if (lit !== null && tabKeys.has(lit)) {
                spans.push({ label: `tab:${lit}`, key: lit, start: n.right.getStart(sf), end: n.right.end })
                return
              }
            }
          }
          ts.forEachChild(n, visitBody)
        }
        visitBody(fn.body)
      }
    }
    ts.forEachChild(node, visitHost)
  }
  visitHost(sf)
  return spans
}

// The app-shell root's label: the same id a shell placement carries (gen-ui-index maps one to the other).
const SHELL_ROOT = SHELL_SURFACE
const APPS_ROOT = 'apps'

function reach(graph, starts, blocked) {
  const seen = new Set()
  const queue = []
  for (const s of starts) if (!blocked.has(s) && !seen.has(s)) { seen.add(s); queue.push(s) }
  while (queue.length) {
    const f = queue.pop()
    for (const e of graph.get(f) ?? []) {
      if (blocked.has(e.to) || seen.has(e.to)) continue
      seen.add(e.to)
      queue.push(e.to)
    }
  }
  return seen
}

function edgeRegions(edge, uses) {
  if (edge.binds.length === 0) return ['*']
  const out = new Set()
  for (const b of edge.binds) for (const r of uses.get(b) ?? []) out.add(r)
  return [...out]
}

/**
 * Which single page each source file is drawn on, from the module graph alone.
 *
 * Roots: each route of the dashboard's route table (`App.tsx`), labelled by the
 * indexed page that route serves (`routeLabel(path)`; `null` ignores a route,
 * e.g. a pop-out frame that re-hosts a page); the app shell (what `main.tsx`
 * and `App.tsx` reach outside every Route element); and every embedded app
 * file under `src/apps/`. A file reached from exactly one root that is an
 * indexed page (`page.*`) maps to that page; reached by two roots (a shared
 * panel, a component the shell also draws) it maps to nothing. A tab host
 * (`tabHosts`: the page root whose `tab === '<key>'` panels were proven by
 * {@link collectTabPanels}) also gives the tab, when the file is reached
 * through that one tab's panel and through no other path from the page.
 *
 * Returns `byFile` (rel -> { page, tab|null }), `owners` (rel -> roots) and
 * `tabFiles` (tab location id -> every file its panel reaches, whether or not
 * other roots reach that file too).
 */
export function buildPageMap({ graph, appRel, mainRel, appUses, routeLabel, tabHosts, isAppFile }) {
  const blocked = new Set([appRel, mainRel])
  const starts = new Map()
  const add = (label, rel) => {
    if (!starts.has(label)) starts.set(label, new Set())
    starts.get(label).add(rel)
  }
  for (const e of graph.get(mainRel) ?? []) if (e.to !== appRel) add(SHELL_ROOT, e.to)
  for (const e of graph.get(appRel) ?? []) {
    for (const r of edgeRegions(e, appUses)) {
      if (r === '*') add(SHELL_ROOT, e.to)
      else {
        const label = routeLabel(r.slice('route:'.length))
        if (label) add(label, e.to)
      }
    }
  }
  for (const rel of graph.keys()) if (isAppFile(rel)) add(APPS_ROOT, rel)
  const owners = new Map()
  for (const [label, s] of starts) {
    for (const f of reach(graph, s, blocked)) {
      if (!owners.has(f)) owners.set(f, new Set())
      owners.get(f).add(label)
    }
  }
  const byFile = new Map()
  const tabFiles = new Map()
  for (const [f, o] of owners) {
    if (o.size !== 1) continue
    const [label] = o
    if (label.startsWith('page.')) byFile.set(f, { page: label, tab: null })
  }
  for (const [host, h] of tabHosts) {
    if (byFile.get(host)?.page !== h.page) continue
    const hostBlocked = new Set([...blocked, host])
    const rest = new Set([...(starts.get(h.page) ?? [])].filter((s) => s !== host))
    const perTab = new Map()
    for (const e of graph.get(host) ?? []) {
      for (const r of edgeRegions(e, h.uses)) {
        if (r === '*') rest.add(e.to)
        else {
          if (!perTab.has(r)) perTab.set(r, new Set())
          perTab.get(r).add(e.to)
        }
      }
    }
    const restReach = reach(graph, rest, hostBlocked)
    const tabReach = [...perTab].map(([r, s]) => [r.slice('tab:'.length), reach(graph, s, hostBlocked)])
    for (const [key, set] of tabReach) {
      if (!h.tabs.has(key)) continue
      const tabId = h.tabs.get(key)
      if (!tabFiles.has(tabId)) tabFiles.set(tabId, new Set())
      for (const f of set) tabFiles.get(tabId).add(f)
    }
    for (const [f, m] of byFile) {
      if (m.page !== h.page || f === host || restReach.has(f)) continue
      const hits = tabReach.filter(([, set]) => set.has(f))
      if (hits.length === 1 && h.tabs.has(hits[0][0])) m.tab = h.tabs.get(hits[0][0])
    }
  }
  return { byFile, owners, tabFiles }
}

/**
 * The root label of each route-table path for {@link buildPageMap}.
 * `pages` are the indexed page locations (`{ id, route }`), `routeFiles` maps a
 * route path to the files its element renders. A route serves the indexed page
 * whose URL it matches; a route two pages match serves neither; a route with
 * no page of its own that renders exactly the files of one page's route
 * (`/apps/-/updates` is DiscoverPage again) is that page. Pop-out and embed
 * frames re-host a page in another window, and a redirect or catch-all
 * renders no page: those return null (not a root at all). Any other route is
 * its own unindexed root, `route:<path>`.
 */
export function makeRouteLabel({ routes, pages, routeFiles }) {
  const pageByRoute = new Map()
  for (const p of pages) {
    const hit = matchRoute(routes, p.route.split('?')[0])
    if (!hit || hit.redirect) continue
    pageByRoute.set(hit.path, pageByRoute.has(hit.path) ? null : p.id)
  }
  const byPath = new Map(routes.map((r) => [r.path, r]))
  const sameFiles = (a, b) => Boolean(a && b && a.size > 0 && a.size === b.size && [...a].every((x) => b.has(x)))
  return (p) => {
    if (p.startsWith('/popout') || p.startsWith('/embed')) return null
    const r = byPath.get(p)
    if (!r || r.redirect || r.catchAll) return null
    const page = pageByRoute.get(p)
    if (page) return page
    if (page === undefined) {
      const twins = [...pageByRoute].filter(([q, id]) => id && sameFiles(routeFiles.get(q), routeFiles.get(p)))
      if (twins.length === 1) return twins[0][1]
    }
    return `route:${p}`
  }
}

const KIND_BY_ROLE = { menuitem: 'menu-item', link: 'link', switch: 'toggle', checkbox: 'toggle' }
const KIND_BY_TAG = {
  a: 'link', Link: 'link', NavLink: 'link', summary: 'disclosure', select: 'field', textarea: 'field',
  MenuItem: 'menu-item', DropdownMenuItem: 'menu-item', ContextMenuItem: 'menu-item',
  Toggle: 'toggle', Switch: 'toggle', Checkbox: 'toggle',
}

/** The location kind of a scanned candidate, from its tag, role and input type only. */
export function candidateKind(c) {
  if (c.role && KIND_BY_ROLE[c.role]) return KIND_BY_ROLE[c.role]
  if (c.tag === 'input') return c.type === 'checkbox' || c.type === 'radio' ? 'toggle' : 'field'
  return KIND_BY_TAG[c.tag] ?? 'button'
}

// ---------------------------------------------------------------- auto tier: render sites, guide policy, markers

/** The build-time marker an audited auto site carries (see autoStampManifest). */
export const AUTO_MARKER_ATTR = 'data-ui-auto'
/**
 * How a guide may treat a location: point at it; point at it with a caution
 * (a destructive control: the guide only highlights it, the person still
 * presses it and its own confirm still asks, and the panel says what it
 * removes is permanent); only return it from find_ui; or never point at it
 * (`deny`: the controls of the agent's own ceiling, see TRUST_ROOT_PARENTS).
 */
export const GUIDE_POLICIES = Object.freeze(['point', 'caution', 'search-only', 'deny'])
/** The two policies a guide may walk. */
export const GUIDABLE_POLICIES = Object.freeze(['point', 'caution'])
/**
 * The reviewed primitives: shared controls that render one interactive element,
 * forward a stamped `data-ui-auto` attribute to it (each pinned by a rendered
 * test in src/guide/autoTargets.test.tsx) and carry any destructive styling
 * as an explicit prop (`danger`, `variant`). An auto site is pointable only
 * when its tag is one of these, imported from that file under that name.
 * `Toggle` is the dashboard's shared switch (`role="switch"`). A
 * DropdownMenuItem only exists while its menu is open, so one stays
 * search-only (`in_container`) until the menu's trigger is registered.
 */
export const AUDITED_PRIMITIVES = Object.freeze({
  Btn: 'src/components/ui.tsx',
  SendBtn: 'src/components/ui.tsx',
  IconButton: 'src/components/ui.tsx',
  Toggle: 'src/components/ui.tsx',
  TabsTrigger: 'src/components/ui/tabs.tsx',
  DropdownMenuItem: 'src/components/ui/dropdown-menu.tsx',
})
/**
 * The removal rule: an English label naming an action that deletes or removes
 * something makes a pointable site `caution` (guidable, with the panel's
 * warning). Only removal: a danger-styled control that removes nothing (Sign
 * out, Deny, Deploy anyway, Clear selection) is plain `point`, since the
 * warning says what it deletes is gone for good. It never makes a site
 * pointable: that takes a reviewed primitive. `REMOVAL_LABEL_RE` in
 * src/kiro_crew/guide_catalog.py is the same rule for `ui.find`.
 */
export const CAUTION_LABEL_RE = /\b(?:delete|remove|uninstall|erase|reset|wipe|purge|destroy)\b|\bclear\s+(?:all|data|everything|history|cache|memory)\b/i
/**
 * The ceiling-label LINT: a label matching it (and not CAUTION_LABEL_RE) widens
 * what the agent may do (approve, grant, trust, allow): listed in the coverage
 * report and kept search-only, and `ui.find` refuses it too
 * (`CEILING_LABEL_RE` in guide_catalog.py). It is a warning, never a grant.
 */
export const SENSITIVE_LABEL_RE = /\b(?:approve|grant|trust|allow|autopilot|yolo)\b/i
/**
 * The agent's own ceiling: tabs no auto site under (or reached through) is
 * ever pointed at, the same tabs `settings.show` refuses (SENSITIVE_TABS in
 * src/guide/guideActions.ts): the security policy, profiles, admission and
 * denied-command rules (Security), Computer Use and credentials (Secrets).
 * Curated descriptors there are judged one by one; an unregistered control
 * there is never guessed safe. Instances (remote crews) is an ordinary tab:
 * reaching another computer is the user's own setup, not this agent's ceiling.
 */
export const TRUST_ROOT_PARENTS = Object.freeze(['settings.tab.security', 'settings.tab.computer-use', 'settings.tab.secrets'])
/** The parent id of an auto control drawn by a file several pages share. */
export const SHARED_PARENT = 'shared'
/** A render-site id: `auto:<page|tab|shell|shared>:<file stem>:<label key>[:<n>]`. */
export const AUTO_SITE_ID_RE = /^auto:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?::[0-9]+)?$/

/** The page, tab, shell or shared parent an auto candidate is drawn under (its location's parent). */
export function autoParentId(c) {
  if (c.page === SHELL_SURFACE) return SHELL_SURFACE
  if (Array.isArray(c.pages)) return SHARED_PARENT
  return c.tab ?? c.page
}

function fileStem(rel) {
  return posixPath.basename(rel).replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_.-]+/g, '-') || 'file'
}

/**
 * The ONE function that names auto render sites: each candidate
 * (`{ page, tab, key, rel, pos }`) gets `auto:<parent>:<file stem>:<label key>`,
 * and when two candidates under one parent would share that (one file drawing
 * the label twice, or two files with one stem), the later ones in (file,
 * position) order get `:2`, `:3`. The result depends only on the set of
 * candidates, never on their input order, so a rebuild of the same tree names
 * every site the same. The generator calls it once; the stamp manifest and the
 * auto plans both carry its output, and the Vite transform only copies it.
 * Search grouping (one location per parent and English label) is separate.
 */
export function autoSiteIds(candidates) {
  const base = (c) => `auto:${autoParentId(c)}:${fileStem(c.rel)}:${c.key}`
  const bases = candidates.map(base)
  const order = candidates.map((_, i) => i).sort((a, b) => {
    if (bases[a] !== bases[b]) return bases[a] < bases[b] ? -1 : 1
    const x = candidates[a]
    const y = candidates[b]
    if (x.rel !== y.rel) return x.rel < y.rel ? -1 : 1
    return x.pos - y.pos
  })
  const seen = new Map()
  const out = new Array(candidates.length)
  for (const i of order) {
    const n = (seen.get(bases[i]) ?? 0) + 1
    seen.set(bases[i], n)
    out[i] = n === 1 ? bases[i] : `${bases[i]}:${n}`
  }
  return out
}

/**
 * One auto site's guide policy, from what the source proves about it:
 * `deny` for a deny-listed site, or one under or reached through the agent's
 * own ceiling (TRUST_ROOT_PARENTS, `c.trustRoot`); otherwise pointable only
 * through a reviewed primitive (`c.primitive`, resolved by the caller from the
 * file's imports), with no prop spread (which could carry or override a
 * marker), outside every closed container and with a well-formed id. A
 * pointable site is `caution` when it removes something (a CAUTION_LABEL_RE
 * label, or listed in `caution`; a `danger` style alone does not), `search-only`
 * when its label only trips the ceiling lint, else `point`. Everything else
 * is `search-only`. `reason` says which rule decided.
 */
export function autoSitePolicy(c, { siteId, deny, caution, english }) {
  if (deny?.has(siteId)) return { policy: 'deny', reason: 'denied' }
  if (c.trustRoot || TRUST_ROOT_PARENTS.includes(autoParentId(c))) return { policy: 'deny', reason: 'trust_root' }
  if (!c.primitive) return { policy: 'search-only', reason: 'not_audited_primitive' }
  if (c.spread) return { policy: 'search-only', reason: 'spread_props' }
  if (c.inContainer) return { policy: 'search-only', reason: 'in_container' }
  if (!AUTO_SITE_ID_RE.test(siteId)) return { policy: 'search-only', reason: 'id_shape' }
  if (caution?.has(siteId)) return { policy: 'caution', reason: 'caution_listed' }
  if (CAUTION_LABEL_RE.test(english ?? '')) return { policy: 'caution', reason: 'caution_label' }
  if (SENSITIVE_LABEL_RE.test(english ?? '')) return { policy: 'search-only', reason: 'sensitive_lint' }
  return { policy: 'point', reason: 'audited_primitive' }
}

/** Placement ids of a shared location's plan, one per candidate page: `pa`, `pb`, ... `pz`, `paa`, ... */
function sharedPlacementId(i) {
  let n = i
  let out = ''
  do {
    out = String.fromCharCode(97 + (n % 26)) + out
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return `p${out}`
}

/**
 * The single-step `ui.show` plan of a guidable auto location: go to its page,
 * point at its one site. A shared location (drawn by a file several pages
 * share) has one placement per candidate page, in its placement order, each
 * pointing at the same site: the page walks the one the person is on when it
 * is a candidate, else the first. A `caution` location's step says so.
 */
export function autoGuidePlan(loc, siteId, { caution = false } = {}) {
  const shared = loc.placements.length > 1
  return {
    version: GUIDE_PLAN_VERSION,
    label_key: loc.label_key,
    placements: loc.placements.map((p, i) => {
      const id = shared ? sharedPlacementId(i) : 'any'
      return {
        id,
        route: p.surface_id === SHELL_SURFACE ? null : p.route,
        steps: [{ id: `${id}:${loc.id}`, location: siteId, label_key: loc.label_key, ...(caution ? { caution: true } : {}) }],
      }
    }),
  }
}

/**
 * The auto tier's own build digest: over the committed `build_digest` it
 * hangs off and every auto plan. The gateway records it on an auto `ui.show`
 * guide and the bundle carries the same value (injected at build from the
 * stamp manifest), so a tab built from another tree refuses (`build_mismatch`).
 */
export function autoBuildDigest(baseBuildDigest, locations) {
  const plans = {}
  for (const l of locations) if (l.guide_plan) plans[l.id] = l.guide_plan
  return `sha256:${createHash('sha256').update(JSON.stringify({ base: baseBuildDigest, plans })).digest('hex')}`
}

/**
 * What the build-time marker step stamps: per source file (website-relative),
 * the sha256 of the exact text the generator scanned and, per pointable site,
 * the offset right after its tag name, the end of its opening tag, its tag and
 * its site id. The Vite transform (scripts/lib/ui-auto-stamp.mjs) inserts
 * `data-ui-auto="<site>"` at each offset and refuses a file whose text no
 * longer hashes the same: it never re-derives an id or re-parses a file.
 */
export function autoStampManifest({ stamps, textOf, buildDigest }) {
  const files = {}
  for (const s of [...stamps].sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : a.tagEnd - b.tagEnd))) {
    const f = (files[s.rel] ??= { sha256: createHash('sha256').update(textOf(s.rel)).digest('hex'), stamps: [] })
    f.stamps.push({ offset: s.tagEnd, open_end: s.openEnd, tag: s.tag, site: s.siteId })
  }
  return { schema_version: 1, attr: AUTO_MARKER_ATTR, build_digest: buildDigest, files }
}

// ---------------------------------------------------------------- index assembly

function settingsRouteParts(route) {
  const path = route.split('?')[0]
  const segs = path.split('/').filter(Boolean)
  return { tab: segs[1], sub: segs[2] }
}

/**
 * Merge the per-area descriptor tables (`src/uiLocations/areas/<area>.ts`, as
 * aggregated by `UI_LOCATION_AREAS` in `descriptors.ts`) into one table.
 *
 * `areas` maps area name -> { id: descriptor }; `areaFiles` lists the stems of
 * the files actually in `areas/`. Refuses an id two areas declare (the
 * aggregator's object spread would silently keep the last), an aggregated area
 * with no file of that name, and a file the aggregator does not list (its
 * locations would never be indexed).
 */
export function mergeLocationAreas(areas, areaFiles) {
  const errors = []
  const descriptors = {}
  const owner = new Map()
  for (const [area, table] of Object.entries(areas ?? {})) {
    if (!areaFiles.includes(area)) errors.push(`UI_LOCATION_AREAS names area '${area}', but there is no src/uiLocations/areas/${area}.ts`)
    for (const [id, d] of Object.entries(table ?? {})) {
      if (owner.has(id)) {
        errors.push(`ui location '${id}' is declared in two areas ('${owner.get(id)}' and '${area}'); one id, one area`)
        continue
      }
      owner.set(id, area)
      descriptors[id] = d
    }
  }
  for (const f of areaFiles) {
    if (!Object.hasOwn(areas ?? {}, f)) errors.push(`src/uiLocations/areas/${f}.ts is not listed in UI_LOCATION_AREAS (descriptors.ts), so none of its locations would be indexed`)
  }
  return { descriptors, errors }
}

// ---------------------------------------------------------------- guide plans (ui.show)

/**
 * Registered destructive or data-replacing controls. The generic `ui.show`
 * guide may point at each one, with a caution: its last step carries
 * `caution: true`, the panel says what it removes is permanent, and pressing
 * the control never finishes the guide by itself (its own confirm decides).
 * Pointing is not clicking; the person still presses it. The generator script
 * fails when an id here is not a registered location (or, for an `auto:` id,
 * a location or render site of this tree), so the list cannot go stale.
 */
export const GUIDE_CAUTION_IDS = Object.freeze([
  'agents.delete',
  'apps.detail.uninstall',
  'apps.library.tile-uninstall',
  'backup.import-file',
  'composer.automation.stop-monitor',
  'notifications.clear-all',
  'notifications.page-clear-all',
  'schedule.cancel-run',
  'schedule.delete',
  'sessions.list-menu.clean-up',
])
/**
 * Registered locations no guide ever points at: a control of the agent's own
 * ceiling (the security policy, profiles, admission, Computer Use, denied
 * commands, credentials, the composer's approval mode), which a guide an
 * agent proposed must never walk the person to. The tabs themselves are
 * refused by TRUST_ROOT_PARENTS and by `settings.show`. A descriptor can also
 * opt out with `guide: false`, which makes it search-only. Checked like
 * GUIDE_CAUTION_IDS.
 */
export const GUIDE_DENY_IDS = Object.freeze(['composer.approval-mode', 'members.permissions'])
/** The only prerequisites a guided location may carry: the guide can satisfy
 *  or check each one. A viewport picks the placement; a reveal is walked; a
 *  preview flag or a gate condition (`UI_GATES`) becomes a `gate` step that
 *  pauses on a blocker naming its setting; a selection condition
 *  (`UI_SELECTION_SCOPES`) becomes a `select` step at its picker; a runtime
 *  predicate (`UI_RUNTIME_PREDICATES`) travels with the reveal step whose
 *  control needs it and shows as a blocker when unmet (one on the location
 *  itself only when an earlier step carries it too). Any other condition on the
 *  location itself (a job is running, an editor section is open) is state the
 *  guide can neither set up nor check, so such a location is never guided. */
const GUIDE_REQUIREMENT_KINDS = new Set(['viewport', 'shown_by', 'preview_flag', 'condition'])
/** The gate id a preview flag compiles to. */
export const previewGateId = (flag) => `preview_flag:${flag}`
/** Reveal steps nest at most this deep (a reveal control revealed by another). */
const GUIDE_MAX_REVEAL_DEPTH = 3
/** Ceiling on one plan's steps, reveals, menus and a preview gate included:
 *  six pointing steps, plus the gate step a preview-held page opens with. */
export const GUIDE_MAX_STEPS = 7
/** The `ui.show` plan format: per-placement step lists with step ids. */
export const GUIDE_PLAN_VERSION = 2

/**
 * Compile the reveal graph: one scope per thing a guide may have to open.
 *
 * - A `shown_by` with `when: <state>` uses the scope DECLARED for that state
 *   (`UI_REVEAL_SCOPES`, whose owner `<GuideRevealScope>` reports it live).
 * - A `shown_by` without `when` is `reveal:<revealer>`.
 * - A registered parent opens `tab:<id>` when its children are reached as a
 *   tab, `menu:<id>` when any is a menu entry, else `open:<id>` (a sheet, a
 *   panel). Compiled ids carry their kind as a prefix.
 *
 * Rejected: a declared scope with an unknown state or a colon in its id, a
 * state with two scopes, a reveal state used with no scope, one state revealed
 * by two different controls, a parent whose children disagree about how it
 * opens, a cycle through a `shown_by`, a placement needing two viewports or two
 * opposite conditions at once.
 */
export function compileRevealScopes({ registered, registeredIds, finalPlacements, inputs, errors }) {
  const declared = inputs.revealScopes ?? {}
  const scopes = {}
  const scopeForState = new Map()
  for (const [scopeId, state] of Object.entries(declared)) {
    if (scopeId.includes(':')) errors.push(`reveal scope '${scopeId}': a declared scope id has no ':' (compiled ids own the 'kind:' prefix)`)
    if (!Object.hasOwn(inputs.revealStates ?? {}, state)) errors.push(`reveal scope '${scopeId}' names unknown reveal state '${state}' (see src/uiLocations/conditions.ts)`)
    else if (scopeForState.has(state)) errors.push(`reveal state '${state}' has two scopes ('${scopeForState.get(state)}', '${scopeId}'); one owner reports it`)
    else scopeForState.set(state, scopeId)
  }
  const revealerOf = new Map()
  const parentKind = new Map()
  const add = (id, entry, where) => {
    const held = scopes[id]
    if (held && held.revealer !== entry.revealer) {
      errors.push(`${where}: reveal scope '${id}' is opened by both '${held.revealer}' and '${entry.revealer}'; one scope has one revealer`)
      return
    }
    scopes[id] = entry
  }
  for (const r of registered) {
    r.d.placements.forEach((p, i) => {
      const where = `ui location '${r.id}' placement ${i}`
      for (const q of p.requires ?? []) {
        if (q.kind !== 'shown_by') continue
        if (q.when === undefined) {
          add(`reveal:${q.location}`, { kind: 'reveal', revealer: q.location }, where)
          continue
        }
        const sid = scopeForState.get(q.when)
        if (sid === undefined) {
          if (Object.hasOwn(inputs.revealStates ?? {}, q.when)) errors.push(`${where}: reveal state '${q.when}' has no scope in UI_REVEAL_SCOPES (src/uiLocations/conditions.ts); every reveal step names one`)
          continue
        }
        const prior = revealerOf.get(q.when)
        if (prior !== undefined && prior !== q.location) {
          errors.push(`${where}: reveal state '${q.when}' is revealed by both '${prior}' and '${q.location}'; one state has one revealer`)
          continue
        }
        revealerOf.set(q.when, q.location)
        add(sid, { kind: 'state', revealer: q.location, state: q.when }, where)
      }
      if (p.parent !== undefined && registeredIds.has(p.parent)) {
        // A tab selects a panel; every other entry is inside the one container
        // the parent opens (a menu, a sheet), whatever part of it the child is in.
        const kind = p.entry === 'tab' ? 'tab' : p.entry === 'menu' ? 'menu' : 'open'
        const prior = parentKind.get(p.parent)
        if (prior !== undefined && (prior === 'tab') !== (kind === 'tab')) {
          errors.push(`${where}: '${p.parent}' is reached both as a tab and as an opened container by its children; one control opens one way`)
          return
        }
        // Inside one container, a menu entry names it a menu.
        parentKind.set(p.parent, prior === 'menu' ? 'menu' : kind)
      }
    })
  }
  const scopeForParent = new Map()
  for (const [pid, kind] of parentKind) {
    const sid = `${kind}:${pid}`
    scopeForParent.set(pid, sid)
    add(sid, { kind, revealer: pid }, `ui location '${pid}'`)
  }
  // A disclosure opens the pane it heads (Older Sessions): its owner, the
  // shared `<GuideDisclosureScope>`, reports `open:<id>` whether or not a
  // registered location sits inside yet.
  for (const r of registered) {
    if (r.d.kind !== 'disclosure' || scopeForParent.has(r.id)) continue
    add(`open:${r.id}`, { kind: 'open', revealer: r.id }, `ui location '${r.id}'`)
  }
  // Cycles through a reveal: A needs B clicked, and B is only reachable through A.
  const edges = new Map()
  for (const r of registered) {
    const out = []
    for (const p of r.d.placements) {
      for (const q of p.requires ?? []) if (q.kind === 'shown_by' && registeredIds.has(q.location)) out.push({ to: q.location, reveal: true })
      if (p.parent !== undefined && registeredIds.has(p.parent)) out.push({ to: p.parent, reveal: false })
    }
    edges.set(r.id, out)
  }
  const state = new Map()
  const reported = new Set()
  const visit = (id, path) => {
    state.set(id, 'open')
    for (const e of edges.get(id) ?? []) {
      if (e.to === id) continue // reported as "shown by itself"
      const at = path.findIndex((s) => s.id === e.to)
      if (state.get(e.to) === 'open' && at >= 0) {
        // Each entry is a node and the edge it leaves by.
        const loop = [...path.slice(at), { id, reveal: e.reveal }]
        // Parent-only loops are already reported as parent cycles.
        if (loop.some((s) => s.reveal)) {
          const names = [...loop.map((s) => s.id), e.to].join(' -> ')
          if (!reported.has(names)) {
            reported.add(names)
            errors.push(`reveal cycle: ${names}`)
          }
        }
        continue
      }
      if (!state.has(e.to)) visit(e.to, [...path, { id, reveal: e.reveal }])
    }
    state.set(id, 'done')
  }
  for (const r of registered) if (!state.has(r.id)) visit(r.id, [])
  // Contradictions inside one resolved placement.
  const opposites = inputs.conditionOpposites ?? []
  for (const r of registered) {
    for (const [i, p] of (finalPlacements.get(r.id) ?? []).entries()) {
      const where = `ui location '${r.id}' placement ${i}`
      const views = new Set(p.requires.filter((q) => q.kind === 'viewport').map((q) => q.value))
      if (views.size > 1) errors.push(`${where}: needs both a desktop and a mobile viewport at once`)
      const conds = new Set(p.requires.filter((q) => q.kind === 'condition').map((q) => q.id))
      for (const [a, b] of opposites) if (conds.has(a) && conds.has(b)) errors.push(`${where}: needs opposite conditions '${a}' and '${b}' at once`)
    }
  }
  const sorted = Object.fromEntries(Object.entries(scopes).sort(([a], [b]) => (a < b ? -1 : 1)))
  return { scopes: sorted, scopeForState, scopeForParent }
}

/**
 * The selection and gate vocabularies a plan may use, checked against the
 * conditions table, the registered locations and the settings registry.
 * `selections`: condition id -> `{ picker, entity }`. `gates`: gate id ->
 * `{ setting_id }` (null when no setting turns it on), including one
 * `preview_flag:<flag>` gate per flag in `PREVIEW_FLAG_ENABLERS`.
 */
export function compileGuideNeeds({ inputs, registeredIds, predicates, errors }) {
  const conditions = inputs.conditions ?? {}
  const settingIds = new Set((inputs.settingsEntries ?? []).map((e) => e.id))
  const gates = new Map()
  for (const [id, g] of Object.entries(inputs.gates ?? {})) {
    if (!Object.hasOwn(conditions, id)) errors.push(`gate '${id}' is not a condition in src/uiLocations/conditions.ts`)
    if (predicates.has(id)) errors.push(`gate '${id}' is also a runtime predicate; a condition is one or the other`)
    const sid = g?.setting ?? null
    if (sid !== null && !settingIds.has(sid)) errors.push(`gate '${id}' names unknown setting '${sid}' (not in the settings registry)`)
    gates.set(id, { setting_id: sid })
  }
  for (const [flag, sid] of Object.entries(inputs.previewEnablers ?? {})) {
    if (!settingIds.has(sid)) errors.push(`preview flag '${flag}' enabler '${sid}' is not in the settings registry`)
    gates.set(previewGateId(flag), { setting_id: sid })
  }
  const selections = new Map()
  for (const [id, s] of Object.entries(inputs.selectionScopes ?? {})) {
    if (!Object.hasOwn(conditions, id)) errors.push(`selection '${id}' is not a condition in src/uiLocations/conditions.ts`)
    if (predicates.has(id) || gates.has(id)) errors.push(`selection '${id}' is also a predicate or a gate; a condition is one of the three`)
    if (!registeredIds.has(s.picker)) errors.push(`selection '${id}' picker '${s.picker}' is not a registered location`)
    if (!/^[a-z]+$/.test(s.entity ?? '')) errors.push(`selection '${id}' entity '${s.entity}' must be one lowercase word (it names catalog keys)`)
    selections.set(id, { picker: s.picker, entity: s.entity })
  }
  return { selections, gates }
}

/**
 * The `ui.show` plan for one registered location, or why it has none.
 *
 * Deny by default: only a curated location (a render-site marker proved where
 * it is drawn) whose every placement needs nothing but a viewport, reveal steps
 * and preview flags, and which is neither destructive nor already covered by
 * its own guide action. Each placement's steps are the controls a person
 * clicks to get there, in order: an ancestor's reveal steps, the registered
 * ancestor itself (a menu button, a sub-tab), the location's own reveal steps,
 * then the location. A page or tab in the path is reached by the placement's
 * route, not a step.
 *
 * Version 2: placements may take different numbers of steps. Each placement
 * has an id (its viewport, or `any`) and each step an id unique in the plan
 * (`<placement>:<key>`); every reveal step names the reveal scope it opens,
 * and every step carries the runtime predicates its control needs.
 *
 * Outermost first: the page (the placement's route), then a `gate` step per
 * gate or preview flag the path needs, then a `select` step per selection
 * (each after its picker's own reveal steps), then the reveal and menu steps,
 * then the location.
 *
 * `ctx`: `{ locations, registeredIds, describedIds, scopeForState,
 * scopeForParent, predicates, selections, gates, deny }`.
 */
/**
 * Runtime predicates the page that draws the location reports as soon as it
 * has loaded (MembersPage: whether the roster has a crewmate). Unlike a
 * control's own transient state, the guide knows it on arrival, so a location
 * needing one may still be planned: its last step carries it, and unmet it is
 * a blocker that says what to do first.
 */
const PAGE_FACT_PREDICATES = new Set(['has_crewmates', 'phone_connect_available'])
/**
 * Runtime predicates the owner of a location's opener reports (the goal and
 * monitor button's popover owner knows whether anything is running). On the
 * location they move to the opener's step, so the guide says there is
 * nothing to stop before it asks anyone to open the panel. Mirrors
 * `UI_OPENER_FACT_PREDICATES` in `src/uiLocations/conditions.ts`.
 */
export const OPENER_FACT_PREDICATES = new Set(['goal_loop_running', 'monitor_running'])

export function guidePlanFor(r, loc, ctx) {
  if (ctx.deny.has(r.id) || placesUnderTrustRoot(loc)) return { reason: 'denied' }
  if (r.d.guide === false) return { reason: 'opted_out' }
  if (r.d.guide) return { reason: 'own_guide_action' }
  if (r.description || loc.label_kind === 'description') return { reason: 'no_label' }
  if (loc.tier && loc.tier !== 'curated') return { reason: 'not_curated' }
  if (!Array.isArray(loc.placements) || loc.placements.length === 0) return { reason: 'no_placement' }
  const selections = ctx.selections ?? new Map()
  const gates = ctx.gates ?? new Map()
  const handled = (id) => !!ctx.predicates?.has(id) || selections.has(id) || gates.has(id)
  const viewportOf = (reqs) => reqs.find((q) => q.kind === 'viewport')?.value
  // A step's control is drawn where the target is: the same route (or the
  // shell, on every page), and no viewport the target placement cannot be in.
  const sameView = (q, route, v) => {
    const vq = viewportOf(q.requires)
    return (q.surface_id === SHELL_SURFACE || q.route === route) && (vq === undefined || vq === v)
  }
  const out = []
  // A control's own runtime conditions become the step's predicates; one the
  // page cannot evaluate makes the whole location search-only.
  let unknownPredicate = false
  const withExtras = (base, scope, requires) => ({
    ...base,
    ...(scope !== undefined ? { scope } : {}),
    ...(requires.length ? { requires } : {}),
  })
  for (const p of loc.placements) {
    if (p.requires.some((q) => !GUIDE_REQUIREMENT_KINDS.has(q.kind) || (q.kind === 'condition' && !handled(q.id)))) return { reason: 'condition' }
    const own = viewportOf(p.requires)
    // A placement drawn at every width whose path differs by viewport (the
    // session picker sits in the sidebar on a desktop, in the drawer on a
    // phone) is planned once per viewport instead.
    let planned = [planPlacement(p, own)]
    if (own === undefined && !planned[0].entry && planned[0].reason === 'unresolvable_step') {
      const split = ['desktop', 'mobile'].map((v) => planPlacement(p, v))
      if (split.every((x) => x.entry)) planned = split
    }
    for (const x of planned) {
      if (!x.entry) return { reason: x.reason }
      out.push(x.entry)
    }
  }
  function planPlacement(p, v) {
    const placementId = v ?? 'any'
    const route = p.surface_id === SHELL_SURFACE ? null : p.route
    let ok = true
    let unknownPredicate = false
    const stepId = (key) => `${placementId}:${key}`
    // Gates and selections any step's control needs, in the order first met.
    const needGates = []
    const needSelections = []
    const need = (id) => {
      const list = gates.has(id) ? needGates : needSelections
      if (!list.includes(id)) list.push(id)
    }
    // `implied`: the control belongs to a conditional reveal (`when`), which is
    // walked only while its state holds; that state already implies the
    // control's own selections and gates (the folded roster means a crewmate
    // is open), so they add no select or gate step of their own.
    const predicatesOf = (reqs, implied = false) => {
      const ids = []
      for (const x of reqs) {
        if (x.kind === 'preview_flag') {
          if (gates.has(previewGateId(x.flag))) need(previewGateId(x.flag))
          else unknownPredicate = true
          continue
        }
        if (x.kind !== 'condition') continue
        if (implied && (gates.has(x.id) || selections.has(x.id))) continue
        if (gates.has(x.id) || selections.has(x.id)) need(x.id)
        else if (!ctx.predicates?.has(x.id)) unknownPredicate = true
        else if (!ids.includes(x.id)) ids.push(x.id)
      }
      return ids
    }
    const pushReveal = (sid, when, depth, into, seen, implied = when !== undefined) => {
      if (seen.has(sid)) return true
      const s = ctx.locations.get(sid)
      if (depth > GUIDE_MAX_REVEAL_DEPTH || !s || !ctx.registeredIds.has(sid) || ctx.describedIds.has(sid)) return false
      const qs = s.placements.filter((q) => sameView(q, p.route, v))
      // A conditional reveal whose control this viewport never draws (the
      // phone's Back to roster on a desktop) is a state this viewport cannot
      // be in, so it is no step here.
      if (qs.length === 0 && when !== undefined && v !== undefined
        && s.placements.length > 0 && s.placements.every((q) => viewportOf(q.requires) !== undefined && viewportOf(q.requires) !== v)) return true
      if (qs.length !== 1) return false
      // A reveal control drawn inside a registered menu (the switcher's "Show
      // the full roster") is reached through that menu: its opener is a step
      // first, with the same scope a menu parent of the target would get.
      const menuParents = qs[0].parent_ids.filter((pid) => ctx.registeredIds.has(pid))
      for (const [k, pid] of menuParents.entries()) {
        if (seen.has(pid)) continue
        const a = ctx.locations.get(pid)
        const prefix = JSON.stringify(qs[0].parent_ids.slice(0, qs[0].parent_ids.indexOf(pid)))
        const aqs = (a?.placements ?? []).filter((q) => sameView(q, p.route, v) && JSON.stringify(q.parent_ids) === prefix)
        const pscope = ctx.scopeForParent?.get(pid)
        if (k > 0 || !a || ctx.describedIds.has(pid) || aqs.length !== 1 || pscope === undefined
          || aqs[0].requires.some((x) => x.kind === 'shown_by')) return false
        seen.add(pid)
        into.push(withExtras({ id: stepId(pid), location: pid, label_key: a.label_key }, pscope, predicatesOf(aqs[0].requires, implied)))
      }
      for (const x of qs[0].requires) {
        if (x.kind === 'shown_by' && !pushReveal(x.location, x.when, depth + 1, into, seen, implied || x.when !== undefined)) return false
      }
      // Every reveal step names the scope it opens (see compileRevealScopes).
      const scope = when !== undefined ? ctx.scopeForState?.get(when) : `reveal:${sid}`
      if (scope === undefined) return false
      seen.add(sid)
      const base = { id: stepId(sid), location: sid, label_key: s.label_key, ...(when !== undefined ? { when } : {}) }
      into.push(withExtras(base, scope, predicatesOf(qs[0].requires, implied)))
      return true
    }
    // The reveal and menu steps, walked first: their controls' conditions say
    // which gates and selections come before them.
    const rest = []
    const seenRest = new Set()
    p.parent_ids.forEach((pid, k) => {
      if (!ok || !ctx.registeredIds.has(pid)) return
      const a = ctx.locations.get(pid)
      const prefix = JSON.stringify(p.parent_ids.slice(0, k))
      const qs = (a?.placements ?? []).filter((q) => sameView(q, p.route, v) && JSON.stringify(q.parent_ids) === prefix)
      const scope = ctx.scopeForParent?.get(pid)
      if (!a || ctx.describedIds.has(pid) || qs.length !== 1 || scope === undefined) {
        ok = false
        return
      }
      for (const x of qs[0].requires) {
        if (x.kind === 'shown_by' && !pushReveal(x.location, x.when, 1, rest, seenRest)) ok = false
      }
      if (ok && !seenRest.has(pid)) {
        seenRest.add(pid)
        rest.push(withExtras({ id: stepId(pid), location: pid, label_key: a.label_key }, scope, predicatesOf(qs[0].requires)))
      }
    })
    for (const x of p.requires) {
      if (ok && x.kind === 'shown_by' && !pushReveal(x.location, x.when, 1, rest, seenRest)) ok = false
    }
    // The location's own conditions: predicates not already carried by an
    // earlier step (an inherited menu's), and the gates and selections it needs.
    const carried = new Set(rest.flatMap((s) => s.requires ?? []))
    const ownPredicates = predicatesOf(p.requires).filter((id) => !carried.has(id))
    // Each selection: its picker's reveal steps, then the select step.
    const front = []
    const seenFront = new Set()
    for (let i = 0; ok && i < needSelections.length; i++) {
      const selId = needSelections[i]
      const { picker, entity } = selections.get(selId)
      const s = ctx.locations.get(picker)
      const qs = s && ctx.registeredIds.has(picker) && !ctx.describedIds.has(picker)
        ? s.placements.filter((q) => sameView(q, p.route, v))
        : []
      // One picker placement on this page, directly on it, behind no other
      // gate or selection (a chain of those is not modelled). The preview that
      // holds the whole page is not another gate: the location needs it too,
      // so its gate step already runs before the select step.
      if (qs.length !== 1 || qs[0].parent_ids.some((pid) => ctx.registeredIds.has(pid))
        || qs[0].requires.some((x) => (x.kind === 'preview_flag' && !needGates.includes(previewGateId(x.flag))) || (x.kind === 'condition' && (gates.has(x.id) || selections.has(x.id))))) {
        ok = false
        break
      }
      for (const x of qs[0].requires) {
        if (ok && x.kind === 'shown_by' && !pushReveal(x.location, x.when, 1, front, seenFront)) ok = false
      }
      if (!ok) break
      const base = { id: stepId(`select:${selId}`), kind: 'select', location: picker, label_key: s.label_key, selection: selId, entity }
      front.push(withExtras(base, undefined, predicatesOf(qs[0].requires)))
    }
    if (!ok) return { reason: 'unresolvable_step' }
    const gateSteps = needGates.map((gid) => {
      const settingId = gates.get(gid).setting_id
      return { id: stepId(`gate:${gid}`), kind: 'gate', gate: gid, ...(settingId ? { setting_id: settingId } : {}) }
    })
    // A reveal a picker needed is walked before the select step, not again after.
    const steps = [...gateSteps, ...front, ...rest.filter((st) => !seenFront.has(st.location))]
    const carriedAll = new Set(steps.flatMap((st) => st.requires ?? []))
    // A runtime predicate on the location itself that no earlier step carries
    // is state the guide only finds out about after pointing at nothing: such
    // a location stays search-only, as before selections and gates.
    // Behind a select step it is different: the pick is what makes the state
    // knowable (the open crewmate keeps a private memory or not), so the
    // predicate travels on the last step and an unmet one is a blocker there.
    // So does a page fact its page always reports once mounted (`PAGE_FACT_PREDICATES`).
    const uncarried = ownPredicates.filter((id) => !carriedAll.has(id))
    // A fact the opener's own owner reports (the goal button knows whether a
    // loop runs) is checked before the opener is pressed: it travels on the
    // opener's step, so an unmet one is a blocker there and the panel is
    // never opened onto nothing to stop.
    const openerId = p.parent_ids[p.parent_ids.length - 1]
    const opener = steps.findLast((st) => st.location === openerId && st.kind === undefined)
    for (const id of uncarried.filter((x) => OPENER_FACT_PREDICATES.has(x))) {
      if (!opener) return { reason: 'condition' }
      opener.requires = [...(opener.requires ?? []), id]
    }
    const stillUncarried = uncarried.filter((id) => !OPENER_FACT_PREDICATES.has(id))
    const afterSelect = steps.some((st) => st.kind === 'select')
    if (stillUncarried.some((id) => !afterSelect && !PAGE_FACT_PREDICATES.has(id))) return { reason: 'condition' }
    steps.push(withExtras({ id: stepId(r.id), location: r.id, label_key: loc.label_key, ...(ctx.caution?.has(r.id) ? { caution: true, ...(loc.caution_key ? { caution_key: loc.caution_key } : {}) } : {}) }, undefined, stillUncarried))
    if (unknownPredicate) return { reason: 'unknown_predicate' }
    if (steps.length > GUIDE_MAX_STEPS) return { reason: 'too_many_steps' }
    return { entry: { id: placementId, route, ...(v !== undefined ? { viewport: v } : {}), steps } }
  }
  // Identical placements (one route reached two equivalent ways) are one plan.
  const placements = out.filter((x, i) => out.findIndex((y) => JSON.stringify(y) === JSON.stringify(x)) === i)
  // The page picks a placement by viewport alone, so each viewport has at most
  // one, and a placement for every viewport stands alone.
  const views = placements.map((x) => x.id)
  if (new Set(views).size !== views.length || (views.includes('any') && views.length > 1)) return { reason: 'ambiguous_placement' }
  return { plan: { version: GUIDE_PLAN_VERSION, label_key: loc.label_key, placements } }
}

/** Whether any placement of *loc* sits under a tab of the agent's own ceiling. */
function placesUnderTrustRoot(loc) {
  return (loc.placements ?? []).some((p) => p.parent_ids.some((pid) => TRUST_ROOT_PARENTS.includes(pid)))
}

/**
 * Assemble the index. `inputs` is plain data; every error is collected and
 * returned rather than thrown so one run names every problem.
 */
export function buildUiIndex(inputs) {
  const errors = []
  const locations = new Map()
  const labelKeysUsed = new Set()
  const english = inputs.catalogs.en

  const keyExists = (key) => key.startsWith(LITERAL_PREFIX) || english[key] !== undefined
  const addLocation = (loc, origin) => {
    if (locations.has(loc.id)) {
      errors.push(`duplicate location id '${loc.id}' (${origin})`)
      return
    }
    if (!keyExists(loc.label_key)) errors.push(`location '${loc.id}': label key '${loc.label_key}' is not in the English catalog`)
    if (loc.label_key.startsWith(DESCRIPTION_PREFIX) !== (loc.label_kind === 'description')) {
      errors.push(`location '${loc.id}': a '${DESCRIPTION_PREFIX}' key is a find_ui description, only for label.from 'description'`)
    }
    const text = english[loc.label_key]
    if (typeof text === 'string' && /\{\{(?!productName\}\})/.test(text)) {
      errors.push(`location '${loc.id}': label '${loc.label_key}' interpolates a value and cannot be quoted as rendered text`)
    }
    locations.set(loc.id, loc)
  }
  const sourceKey = (s) => (s.key !== undefined ? s.key : `${LITERAL_PREFIX}${s.literal}`)

  // Pages ------------------------------------------------------------------
  const previewRequires = (flag, origin) => {
    if (!flag) return []
    if (!inputs.previewEnablers[flag]) {
      errors.push(`${origin}: preview flag '${flag}' has no enabling setting in PREVIEW_FLAG_ENABLERS`)
      return []
    }
    return [{ kind: 'preview_flag', flag, location: `setting:${inputs.previewEnablers[flag]}` }]
  }
  const navPaths = new Set(inputs.navPaths ?? [])
  const pinnableTabs = new Set()
  const surfaceIds = new Set()
  for (const s of inputs.surfaces) {
    if (s.pinnable) {
      const m = /^\/capabilities\?tab=([a-z-]+)$/.exec(s.route)
      if (!m) errors.push(`pinnable surface '${s.navId}' is not a Customize tab route`)
      else pinnableTabs.add(m[1])
      continue
    }
    if (s.navId === SHELL_SURFACE) errors.push(`surface '${s.navId}' collides with the reserved shell surface id`)
    surfaceIds.add(s.navId)
    const requires = [...previewRequires(s.previewFlag, `surface '${s.navId}'`)]
    if (s.appOnly) requires.push({ kind: 'condition', id: 'app_enabled' })
    addLocation({
      id: `page.${s.navId}`,
      kind: 'page',
      label_key: s.labelKey,
      placements: [{
        surface_id: s.navId,
        route: s.route,
        parent_ids: [],
        // An app-only page is a rail row while its app is on (the condition
        // above); a page hidden from the registry's rows is one only when the
        // shell draws its NavItem by hand (`navPaths`).
        entry_kind: s.appOnly || !s.hiddenFromNav || navPaths.has(s.route) ? 'rail' : 'direct-link',
        requires,
      }],
    }, 'surface')
  }
  const routesTaken = new Set(inputs.surfaces.map((s) => s.route))
  const routeTable = inputs.routes ?? []
  const legacy = inputs.legacyPages ?? {}
  const legacyAliases = []
  for (const p of inputs.extraPages) {
    if (routesTaken.has(p.route)) continue
    const key = inputs.extraTitleKeys[p.key]
    if (!key) {
      errors.push(`extra page '${p.key}' has no title key`)
      continue
    }
    const landed = matchRoute(routeTable, p.route)
    if (landed?.redirect) {
      // A route that only redirects is never an answer: its title searches the
      // location the redirect lands on, which keeps its own route and gates.
      if (!legacy[p.key]) {
        errors.push(`extra page '${p.key}' route '${p.route}' redirects to '${landed.redirect}'; name its canonical location in LEGACY_PAGE_CANONICAL`)
      } else legacyAliases.push({ page: p, key, canonical: legacy[p.key], target: landed.redirect })
      continue
    }
    if (legacy[p.key]) errors.push(`LEGACY_PAGE_CANONICAL names '${p.key}', whose route '${p.route}' does not redirect; remove the mapping`)
    surfaceIds.add(p.key)
    addLocation({
      id: `page.${p.key}`,
      kind: 'page',
      label_key: key,
      placements: [{
        surface_id: p.key, route: p.route, parent_ids: [], entry_kind: navPaths.has(p.route) ? 'rail' : 'direct-link',
        requires: previewRequires(p.previewFlag, `extra page '${p.key}'`),
      }],
    }, 'extra page')
  }
  for (const k of Object.keys(legacy)) {
    if (!inputs.extraPages.some((p) => p.key === k)) errors.push(`LEGACY_PAGE_CANONICAL names unknown extra page '${k}'`)
  }

  // Tabs -------------------------------------------------------------------
  for (const t of inputs.capabilityTabs) {
    addLocation({
      id: `tab.capabilities.${t.id}`,
      kind: 'tab',
      label_key: sourceKey(t),
      ...(pinnableTabs.has(t.id) ? { pinnable: true } : {}),
      placements: [{
        surface_id: 'capabilities', route: `/capabilities?tab=${t.id}`, parent_ids: ['page.capabilities'],
        entry_kind: 'tab', requires: [],
      }],
    }, 'Customize tab')
  }
  // Tabs of other SidePanelLayout pages (Developer). Always the explicit
  // `?tab=` route: the bare page route restores whichever tab was open last.
  for (const [surface, tabs] of Object.entries(inputs.pageTabs ?? {})) {
    const page = locations.get(`page.${surface}`)
    if (!page) {
      errors.push(`page tabs for unknown page '${surface}'`)
      continue
    }
    for (const t of tabs) {
      addLocation({
        id: `tab.${surface}.${t.id}`,
        kind: 'tab',
        label_key: sourceKey(t),
        placements: page.placements.map((p) => ({
          surface_id: p.surface_id, route: `${p.route}?tab=${t.id}`, parent_ids: [page.id],
          entry_kind: 'tab', requires: [...p.requires],
        })),
      }, `${surface} tab`)
    }
  }
  for (const id of pinnableTabs) {
    if (!inputs.capabilityTabs.some((t) => t.id === id)) errors.push(`pinnable surface names Customize tab '${id}', which the page does not render`)
  }
  for (const t of inputs.settingsTabs) {
    addLocation({
      id: `settings.tab.${t.id}`,
      kind: 'tab',
      label_key: sourceKey(t),
      placements: [{
        surface_id: 'settings', route: `/settings/${t.id}`, parent_ids: ['page.settings'], entry_kind: 'tab',
        requires: previewRequires(inputs.settingsTabPreview[t.id], `Settings tab '${t.id}'`),
      }],
    }, 'Settings tab')
  }
  for (const [tab, subs] of Object.entries(inputs.settingsSubs)) {
    if (!locations.has(`settings.tab.${tab}`)) errors.push(`Settings sub-pages for unknown tab '${tab}'`)
    for (const s of subs) {
      addLocation({
        id: `settings.sub.${tab}.${s.id}`,
        kind: 'tab',
        label_key: sourceKey(s),
        placements: [{
          surface_id: 'settings', route: `/settings/${tab}/${s.id}`,
          parent_ids: ['page.settings', `settings.tab.${tab}`], entry_kind: 'tab', requires: [],
        }],
      }, 'Settings sub-page')
    }
  }

  // Settings -----------------------------------------------------------------
  const agentById = new Map(inputs.agentSettings.map((a) => [a.id, a]))
  const settingIds = new Set()
  for (const e of inputs.settingsEntries) {
    const agent = agentById.get(e.id)
    if (!agent) {
      errors.push(`setting '${e.id}' is in settingsRegistry.gen.ts but not the agent registry; run npm run gen:settings`)
      continue
    }
    if (!e.labelKey) {
      errors.push(`setting '${e.id}' has no labelKey; its label cannot be localized`)
      continue
    }
    settingIds.add(e.id)
    const { tab, sub } = settingsRouteParts(agent.route)
    const tabId = `settings.tab.${tab}`
    const subId = sub ? `settings.sub.${tab}.${sub}` : null
    if (!locations.has(tabId)) errors.push(`setting '${e.id}': route tab '${tab}' is not a Settings tab`)
    if (subId && !locations.has(subId)) errors.push(`setting '${e.id}': route sub-page '${tab}/${sub}' is not a known Settings sub-page`)
    addLocation({
      id: `setting:${e.id}`,
      kind: 'setting',
      label_key: e.labelKey,
      setting_id: e.id,
      placements: [{
        surface_id: 'settings', route: agent.route,
        parent_ids: ['page.settings', tabId, ...(subId ? [subId] : [])], entry_kind: 'tab', requires: [],
      }],
    }, 'setting')
  }
  for (const a of inputs.agentSettings) {
    if (!settingIds.has(a.id) && !inputs.settingsEntries.some((e) => e.id === a.id)) {
      errors.push(`setting '${a.id}' is in the agent registry but not settingsRegistry.gen.ts; run npm run gen:settings`)
    }
  }
  for (const [flag, sid] of Object.entries(inputs.previewEnablers)) {
    if (!settingIds.has(sid)) errors.push(`PREVIEW_FLAG_ENABLERS['${flag}'] names unknown setting '${sid}'`)
  }

  // Legacy page titles become aliases of where their redirect lands --------------
  const pathOf = (url) => url.split('?')[0].split('#')[0].replace(/\/+$/, '') || '/'
  for (const a of legacyAliases) {
    const loc = locations.get(a.canonical)
    const where = `LEGACY_PAGE_CANONICAL['${a.page.key}']`
    if (!loc) {
      errors.push(`${where} names unknown location '${a.canonical}'`)
      continue
    }
    const route = loc.placements[0]?.route ?? ''
    const [, targetQuery] = a.target.split('?')
    const [, routeQuery] = route.split('?')
    if (pathOf(route) !== pathOf(a.target) || (targetQuery && targetQuery !== routeQuery)) {
      errors.push(`${where}: '${a.page.route}' redirects to '${a.target}', but '${a.canonical}' is at '${route}'`)
      continue
    }
    loc.alias_keys = [...new Set([...(loc.alias_keys ?? []), a.key])]
  }

  // Every route a location hands out must be one the router renders ------------
  if (!Array.isArray(inputs.routes) || inputs.routes.length === 0) errors.push('no route table: pass the routes read from App.tsx')
  const appRoutes = new Set(inputs.surfaces.filter((s) => s.appOnly).map((s) => pathOf(s.route)))
  const checkRoute = (url, where) => {
    if (typeof url !== 'string' || !url.startsWith('/')) {
      errors.push(`${where}: route '${url ?? ''}' is empty or not an absolute path`)
      return
    }
    const hit = matchRoute(routeTable, url)
    if (hit?.redirect) errors.push(`${where}: route '${url}' only redirects (to '${hit.redirect}'); name the location it lands on`)
    // Built-in apps are served by the `/:builtinApp/*` arm, keyed by their surface route.
    else if (!hit && !appRoutes.has(`/${pathOf(url).split('/').filter(Boolean)[0] ?? ''}`)) {
      errors.push(`${where}: route '${url}' is not in the dashboard route table (App.tsx)`)
    }
  }

  // Registered controls --------------------------------------------------------
  const sitesById = new Map()
  for (const site of inputs.markerSites) {
    if (!inputs.descriptors[site.id]) {
      errors.push(`${site.rel}:${site.line}: unknown ui location '${site.id}' — add it to its area file in src/uiLocations/areas/`)
      continue
    }
    if (sitesById.has(site.id)) {
      const first = sitesById.get(site.id)
      errors.push(`ui location '${site.id}' is marked twice (${first.rel}:${first.line}, ${site.rel}:${site.line}); one id names one render site`)
      continue
    }
    sitesById.set(site.id, site)
  }
  const registered = []
  for (const [id, d] of Object.entries(inputs.descriptors)) {
    if (RESERVED_PREFIXES.some((p) => id.startsWith(p)) || !LOCATION_ID_RE.test(id)) {
      errors.push(`ui location id '${id}' must be lowercase dotted/hyphenated and not start with ${RESERVED_PREFIXES.join(', ')}`)
      continue
    }
    if (!KINDS.has(d.kind)) errors.push(`ui location '${id}': unknown kind '${d.kind}'`)
    const site = sitesById.get(id)
    if (!site) {
      errors.push(`ui location '${id}' is described but no render site carries {...uiLocation('${id}')}`)
      continue
    }
    const label = site.resolved
    if (label.error) {
      errors.push(label.error)
      continue
    }
    if (!Array.isArray(d.placements) || d.placements.length === 0) {
      errors.push(`ui location '${id}' has no placements`)
      continue
    }
    for (const k of d.aliasKeys ?? []) {
      if (!keyExists(k)) errors.push(`ui location '${id}': alias key '${k}' is not in the English catalog`)
      if (k.startsWith(DESCRIPTION_PREFIX)) errors.push(`ui location '${id}': alias key '${k}' is a find_ui description, not on-screen text`)
    }
    if (d.cautionKey !== undefined) {
      if (typeof d.cautionKey !== 'string' || !keyExists(d.cautionKey)) errors.push(`ui location '${id}': caution key '${d.cautionKey}' is not in the English catalog`)
      else if (!(inputs.guideCaution ?? GUIDE_CAUTION_IDS).includes(id)) errors.push(`ui location '${id}': cautionKey is only for a location in GUIDE_CAUTION_IDS`)
    }
    if (label.description) {
      // Authored for find_ui alone, so every shipped locale must carry it.
      const key = label.source.key
      const absent = inputs.locales.filter((l) => typeof inputs.catalogs[l]?.[key] !== 'string' || !inputs.catalogs[l][key].trim())
      if (absent.length) errors.push(`ui location '${id}': description '${key}' is missing in ${absent.join(', ')}`)
    }
    registered.push({ id, d, labelKey: sourceKey(label.source), site, excluded: label.excluded, description: !!label.description })
  }
  for (const r of registered) {
    addLocation({
      id: r.id,
      kind: r.d.kind,
      label_key: r.labelKey,
      // A description says what the control is; its on-screen label is runtime data.
      ...(r.description ? { label_kind: 'description' } : {}),
      ...(r.d.aliasKeys?.length ? { alias_keys: [...r.d.aliasKeys] } : {}),
      // The page's own words for what a destructive control removes and keeps.
      ...(typeof r.d.cautionKey === 'string' ? { caution_key: r.d.cautionKey } : {}),
      // Resolved below, once every registered id exists to be a parent.
      placements: r.d.placements.map((p) => ({ ...p })),
      source: { file: r.site.rel, line: r.site.line },
    }, 'descriptor')
  }

  // Placements for registered controls, resolved whole and in dependency
  // order: a parent's placements are final before a child picks one, whatever
  // order the descriptors were declared in. The child inherits the chosen
  // parent placement's path, route and requirements.
  const registeredIds = new Set(registered.map((r) => r.id))
  // A description location has no label to quote, so it can never be a step in
  // someone else's path: nothing hangs under it and nothing is shown_by it.
  const describedIds = new Set(registered.filter((r) => r.description).map((r) => r.id))
  for (const r of registered) {
    r.d.placements.forEach((p, i) => {
      const where = `ui location '${r.id}' placement ${i}`
      if (describedIds.has(p.parent)) errors.push(`${where}: parent '${p.parent}' has only a description, no label to put in a path`)
      for (const q of p.requires ?? []) {
        if (q.kind === 'shown_by' && describedIds.has(q.location)) errors.push(`${where}: shown_by '${q.location}' has only a description, no label to name the step`)
      }
    })
  }
  const finalPlacements = new Map()
  const viewportOf = (reqs) => reqs.find((q) => q.kind === 'viewport')?.value
  const sameShape = (a, b) => JSON.stringify([a.route, a.parent_ids, a.requires]) === JSON.stringify([b.route, b.parent_ids, b.requires])
  const ownRequires = (r, p, where) => {
    const requires = []
    for (const req of p.requires ?? []) {
      if (!REQUIREMENT_KINDS.has(req.kind)) {
        errors.push(`${where}: unknown requirement kind '${req.kind}'`)
        continue
      }
      if (req.kind === 'shown_by') {
        if (!locations.has(req.location)) errors.push(`${where}: shown_by names unknown location '${req.location}'`)
        if (req.location === r.id) errors.push(`${where}: a location cannot be shown by itself`)
        if (req.when !== undefined && !Object.hasOwn(inputs.revealStates ?? {}, req.when)) {
          errors.push(`${where}: unknown reveal state '${req.when}' (add it to src/uiLocations/conditions.ts)`)
        }
        requires.push({ kind: 'shown_by', location: req.location, ...(req.when !== undefined ? { when: req.when } : {}) })
      } else if (req.kind === 'viewport') {
        if (req.value !== 'desktop' && req.value !== 'mobile') errors.push(`${where}: viewport must be desktop or mobile`)
        requires.push({ kind: 'viewport', value: req.value })
      } else if (req.kind === 'condition') {
        if (!Object.hasOwn(inputs.conditions ?? {}, req.id)) errors.push(`${where}: unknown condition '${req.id}' (add it to src/uiLocations/conditions.ts)`)
        requires.push({ kind: 'condition', id: req.id })
      } else {
        requires.push(...previewRequires(req.flag, where))
      }
    }
    return requires
  }
  // The parent placement a child hangs under: same surface, no conflicting
  // viewport, and one shape (or the one named by parentPlacement).
  const pickParent = (p, own, parentPlacements, where) => {
    const v = viewportOf(own)
    const compatible = parentPlacements
      .map((q, qi) => ({ q, qi }))
      .filter(({ q }) => q.surface_id === p.surface && (v === undefined || !q.requires.some((x) => x.kind === 'viewport' && x.value !== v)))
    if (p.parentPlacement !== undefined) {
      const hit = compatible.find(({ qi }) => qi === p.parentPlacement)?.q ?? null
      if (!hit) errors.push(`${where}: parentPlacement ${p.parentPlacement} is not a placement of '${p.parent}' on surface '${p.surface}' compatible with this one`)
      return hit
    }
    if (compatible.length === 0) {
      errors.push(`${where}: parent '${p.parent}' has no placement on surface '${p.surface}' compatible with this one`)
      return null
    }
    if (compatible.some(({ q }) => !sameShape(q, compatible[0].q))) {
      errors.push(`${where}: parent '${p.parent}' has ${compatible.length} different compatible placements; add a viewport requirement or name parentPlacement`)
      return null
    }
    return compatible[0].q
  }
  // The parent's requirements, then the child's own, each once.
  const mergeRequires = (inherited, own) => {
    const requires = []
    const seen = new Set()
    for (const q of [...inherited, ...own]) {
      const k = JSON.stringify(q)
      if (!seen.has(k)) {
        seen.add(k)
        requires.push(q)
      }
    }
    return requires
  }
  const resolvePlacements = (id, stack) => {
    if (!registeredIds.has(id)) return locations.get(id)?.placements ?? null
    if (finalPlacements.has(id)) return finalPlacements.get(id)
    if (stack.includes(id)) {
      errors.push(`ui location parent cycle: ${[...stack, id].join(' -> ')}`)
      return null
    }
    const r = registered.find((x) => x.id === id)
    const out = []
    r.d.placements.forEach((p, i) => {
      const where = `ui location '${id}' placement ${i}`
      if (p.surface !== SHELL_SURFACE && !surfaceIds.has(p.surface)) errors.push(`${where}: unknown surface '${p.surface}'`)
      if (!ENTRY_KINDS.has(p.entry)) errors.push(`${where}: unknown entry kind '${p.entry}'`)
      const own = ownRequires(r, p, where)
      if (p.surface === SHELL_SURFACE) {
        // Shell chrome is on every page: no route. It may hang under another
        // shell control (a row in the phone menu, under the menu button), whose
        // placement it inherits like a page child does; never under a page.
        if (p.route !== undefined) {
          errors.push(`${where}: a '${SHELL_SURFACE}' placement takes no route (it is drawn on every page)`)
          return
        }
        if (p.parent === undefined) {
          if (p.parentPlacement !== undefined) errors.push(`${where}: parentPlacement needs a parent`)
          else out.push({ surface_id: SHELL_SURFACE, route: '', parent_ids: [], entry_kind: p.entry, requires: own })
          return
        }
        const parentPlacements = registeredIds.has(p.parent) ? resolvePlacements(p.parent, [...stack, id]) : null
        if (!parentPlacements || parentPlacements.length === 0 || parentPlacements.some((q) => q.surface_id !== SHELL_SURFACE)) {
          errors.push(`${where}: a '${SHELL_SURFACE}' placement can only hang under another registered shell location, not '${p.parent}'`)
          return
        }
        const chosen = pickParent(p, own, parentPlacements, where)
        if (!chosen) return
        out.push({
          surface_id: SHELL_SURFACE, route: '', parent_ids: [...chosen.parent_ids, p.parent], entry_kind: p.entry,
          requires: mergeRequires(chosen.requires, own),
        })
        return
      }
      if (!locations.has(p.parent)) {
        errors.push(`${where}: unknown parent '${p.parent}'`)
        return
      }
      const parentPlacements = resolvePlacements(p.parent, [...stack, id])
      if (!parentPlacements) return
      const chosen = pickParent(p, own, parentPlacements, where)
      if (!chosen) return
      const route = p.route ?? chosen.route
      checkRoute(route, where)
      out.push({
        surface_id: p.surface,
        route: route ?? '',
        parent_ids: [...chosen.parent_ids, p.parent],
        entry_kind: p.entry,
        requires: mergeRequires(chosen.requires, own),
      })
    })
    finalPlacements.set(id, out)
    return out
  }
  for (const r of registered) {
    const loc = locations.get(r.id)
    if (!loc) continue
    loc.placements = resolvePlacements(r.id, []) ?? []
    if (loc.placements.length === 0) errors.push(`ui location '${r.id}' has no placement that resolves`)
    if (r.d.guide) {
      const g = r.d.guide
      const params = g.params ?? {}
      const verdict = inputs.resolveGuide ? inputs.resolveGuide(g.action, params) : { ok: false, reason: 'no guide catalog was given' }
      if (!verdict.ok) errors.push(`ui location '${r.id}': guide binding ${g.action} is refused by the guide catalog (${verdict.reason})`)
      else loc.guide_ref = { action_id: g.action, ...(g.params ? { params: g.params } : {}) }
    }
  }
  // The generic `ui.show` plan, once every placement is final (see guidePlanFor),
  // over the compiled reveal graph (see compileRevealScopes).
  const { scopes: revealScopes, scopeForState, scopeForParent } = compileRevealScopes({ registered, registeredIds, finalPlacements, inputs, errors })
  const predicates = new Set(inputs.runtimePredicates ?? [])
  for (const id of predicates) {
    if (!Object.hasOwn(inputs.conditions ?? {}, id)) errors.push(`runtime predicate '${id}' is not a condition in src/uiLocations/conditions.ts`)
  }
  const { selections, gates } = compileGuideNeeds({ inputs, registeredIds, predicates, errors })
  const guideCtx = { locations, registeredIds, describedIds, scopeForState, scopeForParent, predicates, selections, gates, deny: new Set(inputs.guideDeny ?? GUIDE_DENY_IDS), caution: new Set(inputs.guideCaution ?? GUIDE_CAUTION_IDS) }
  for (const r of registered) {
    const loc = locations.get(r.id)
    if (!loc || loc.placements.length === 0) continue
    const verdict = guidePlanFor(r, loc, guideCtx)
    if (verdict.plan) loc.guide_plan = verdict.plan
    // A curated location is pointable (with a caution when it is destructive)
    // unless it is denied or opts out; a plan may still be missing for a
    // prerequisite the guide cannot walk.
    loc.guide_policy = verdict.reason === 'denied' ? 'deny'
      : verdict.reason === 'opted_out' ? 'search-only'
        : guideCtx.caution.has(r.id) ? 'caution' : 'point'
  }
  // A shell placement is searchable by its own surface id, like a page's.
  if ([...finalPlacements.values()].some((ps) => ps.some((p) => p.surface_id === SHELL_SURFACE))) surfaceIds.add(SHELL_SURFACE)
  // Generated placements were built before the route table check existed.
  for (const loc of locations.values()) {
    if (registeredIds.has(loc.id)) continue
    loc.placements.forEach((p, i) => {
      checkRoute(p.route, `location '${loc.id}' placement ${i}`)
      for (const q of p.requires) {
        if (q.kind === 'condition' && !Object.hasOwn(inputs.conditions ?? {}, q.id)) {
          errors.push(`location '${loc.id}': unknown condition '${q.id}' (add it to src/uiLocations/conditions.ts)`)
        }
      }
    })
  }

  // Search terms ---------------------------------------------------------------
  // Registered locations carry them in their descriptor, generated ones in the
  // SEARCH_TERMS overlay keyed by stable id; never both, and never a guess.
  const termSources = []
  for (const r of registered) if (r.d.terms !== undefined) termSources.push([r.id, r.d.terms, 'descriptor'])
  for (const [id, terms] of Object.entries(inputs.searchTerms ?? {})) {
    if (!locations.has(id)) {
      errors.push(`SEARCH_TERMS names unknown location '${id}'`)
      continue
    }
    if (inputs.descriptors[id]) {
      errors.push(`SEARCH_TERMS names registered location '${id}'; put its terms in its descriptor`)
      continue
    }
    termSources.push([id, terms, 'SEARCH_TERMS'])
  }
  for (const [id, terms, origin] of termSources) {
    const loc = locations.get(id)
    if (!loc) continue
    const checked = validateTerms(id, terms, origin, loc, inputs, errors)
    if (checked) loc.terms = checked
  }

  // Auto tier ------------------------------------------------------------------
  // Unregistered controls whose one static label key and one page are proven
  // from source (see buildPageMap). Never a guess: no search terms, no
  // prerequisites of their own (`conditions_unknown`), and a label key that a
  // generated or registered location already shows is left to that location.
  const auto = buildAutoLocations(inputs, locations, errors, addLocation)

  // Labels -------------------------------------------------------------------
  for (const loc of locations.values()) {
    labelKeysUsed.add(loc.label_key)
    for (const k of loc.alias_keys ?? []) labelKeysUsed.add(k)
    if (loc.caution_key) labelKeysUsed.add(loc.caution_key)
  }
  const labels = {}
  const missing = {}
  for (const locale of inputs.locales) {
    const cat = inputs.catalogs[locale] ?? {}
    const out = {}
    for (const key of [...labelKeysUsed].sort()) {
      if (key.startsWith(LITERAL_PREFIX)) {
        if (locale === 'en') out[key] = key.slice(LITERAL_PREFIX.length)
        continue
      }
      const v = cat[key]
      if (typeof v === 'string' && v.trim()) out[key] = v.replaceAll('{{productName}}', inputs.productName)
      else if (locale !== 'en') (missing[locale] ??= []).push(key)
    }
    labels[locale] = out
  }

  const sorted = [...locations.values()].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  const counts = {}
  // The auto tier is only counted when it was asked for: the committed index is
  // built without it (it is a build-time artifact, see buildAutoArtifact), so
  // its coverage block never moves when an unregistered control is added.
  const withAuto = Array.isArray(inputs.autoCandidates)
  const tiers = withAuto ? { generated: 0, curated: 0, auto: 0 } : { generated: 0, curated: 0 }
  for (const l of sorted) {
    if (!l.tier) l.tier = registeredIds.has(l.id) ? 'curated' : 'generated'
    tiers[l.tier]++
    if (l.tier !== 'auto') counts[l.kind] = (counts[l.kind] ?? 0) + 1
  }
  const index = {
    $comment: 'AUTO-GENERATED by website/scripts/gen-ui-index.mjs from the dashboard source — DO NOT EDIT. '
      + 'Regenerate with `npm run gen:ui`; read by the find_ui tool (kiro_crew/mcp_guide.py).',
    schema_version: SCHEMA_VERSION,
    input_digest: inputs.inputDigest,
    // Filled below from what the browser bundle and the gateway must agree on.
    build_digest: '',
    coverage: {
      scope: `pages, page tabs, Settings tabs and sub-pages, every Settings control, registered controls${withAuto ? `, and ${AUTO_SCOPE}` : ''}; `
        + 'not every dashboard control',
      counts,
      tiers,
      registered_controls: registered.length,
      ...(withAuto ? { auto_controls: tiers.auto } : {}),
    },
    locales: [...inputs.locales],
    surfaces: [...surfaceIds].sort(),
    // What each prerequisite id means, from src/uiLocations/conditions.ts.
    conditions: { ...(inputs.conditions ?? {}) },
    reveal_states: { ...(inputs.revealStates ?? {}) },
    reveal_scopes: Object.fromEntries(Object.entries(revealScopes).sort(([a], [b]) => (a < b ? -1 : 1))),
    // Conditions a guide step may carry as a live predicate (a browser evaluator exists).
    runtime_predicates: [...predicates].sort(),
    // Selection scopes (a `select` step points at the picker until the page
    // reports the fact) and gates (a `gate` step pauses on a blocker naming
    // the setting that turns it on). Both are also live predicates.
    guide_selections: Object.fromEntries([...selections].sort(([a], [b]) => (a < b ? -1 : 1))),
    guide_gates: Object.fromEntries([...gates].sort(([a], [b]) => (a < b ? -1 : 1))),
    locations: sorted.map(({ source, ...rest }) => rest),
    labels,
  }
  index.build_digest = guideBuildDigest(index)
  return { index, errors, missing, registered, auto }
}

/**
 * The ONE digest the browser bundle (`guidePlans.gen.ts` `GUIDE_BUILD_DIGEST`)
 * and the packaged index (`build_digest`) both carry: over exactly what a live
 * guide relies on the two halves agreeing about -- the curated location ids a
 * tab may be asked to observe, the reveal scopes, and every `ui.show` plan. A
 * gateway stores it on each `ui.show` record it accepts, and a tab whose
 * bundle carries another refuses the guide (`build_mismatch`) instead of
 * pointing at what may no longer be there.
 */
export function guideBuildDigest(index) {
  const plans = {}
  for (const l of index.locations) if (l.guide_plan) plans[l.id] = l.guide_plan
  const payload = JSON.stringify({
    schema: index.schema_version,
    observable: guideObservableIds(index),
    scopes: index.reveal_scopes ?? {},
    selections: index.guide_selections ?? {},
    gates: index.guide_gates ?? {},
    plans,
  })
  return `sha256:${createHash('sha256').update(payload).digest('hex')}`
}

/** Curated location ids, sorted: the only ids a live observation may name. */
export function guideObservableIds(index) {
  return index.locations.filter((l) => l.tier === 'curated').map((l) => l.id).sort()
}

/**
 * Auto locations from `inputs.autoCandidates` (`{ page, tab, key, kind, rel }`,
 * already mapped to one page by the caller). One location per (page or tab,
 * label key); `skipped` counts why a candidate was left out.
 */
/** Stands in for the shell among a shared control's parents (it has no location). */
const SHELL_PLACEMENT_PARENT = Object.freeze({ tier: 'shell', placements: [] })

function buildAutoLocations(inputs, locations, errors, addLocation) {
  const shown = new Set()
  for (const loc of locations.values()) {
    shown.add(loc.label_key)
    for (const k of loc.alias_keys ?? []) shown.add(k)
  }
  const skipped = { shown_by_curated: 0, untranslated: 0, interpolated: 0, not_in_catalog: 0 }
  let covered = 0
  // One location per (page or tab, English label): seven channel panels that
  // each say "Setup guide" are one answer, not seven identical paths. Each
  // location keeps every render site it groups, with that site's policy.
  const groups = new Map()
  const english = inputs.catalogs.en
  const deny = new Set(inputs.guideDeny ?? GUIDE_DENY_IDS)
  const caution = new Set(inputs.guideCaution ?? GUIDE_CAUTION_IDS)
  for (const c of inputs.autoCandidates ?? []) {
    const shell = c.page === SHELL_SURFACE
    const parentId = autoParentId(c)
    // A shared control hangs under each page that draws it, in that order.
    // (the shell, when it draws it too, last: on every page, no route).
    const parentIds = shell ? [] : parentId === SHARED_PARENT ? c.pages : [parentId]
    const parents = parentIds.map((id) => (id === SHELL_SURFACE ? SHELL_PLACEMENT_PARENT : locations.get(id)))
    const unknown = parentIds.find((id, i) => !parents[i] || parents[i].tier === 'auto')
    if (unknown !== undefined || (parentId === SHARED_PARENT && parentIds.length < 2)) {
      errors.push(`auto candidate at ${c.rel}: maps to unknown location '${unknown ?? parentId}'`)
      continue
    }
    const placementsUnder = () => parents.flatMap((parent, i) => (parent === SHELL_PLACEMENT_PARENT
      ? [{ surface_id: SHELL_SURFACE, route: '', parent_ids: [], entry_kind: 'content', requires: [] }]
      : parent.placements.map((p) => ({
        surface_id: p.surface_id, route: p.route, parent_ids: [...p.parent_ids, parentIds[i]],
        entry_kind: 'content', requires: p.requires.map((q) => ({ ...q })),
      }))))
    if (typeof english[c.key] !== 'string' || c.key.startsWith(DESCRIPTION_PREFIX)) { skipped.not_in_catalog++; continue }
    if (shown.has(c.key)) { skipped.shown_by_curated++; continue }
    if (/\{\{(?!productName\}\})/.test(english[c.key])) { skipped.interpolated++; continue }
    if (inputs.locales.some((l) => typeof inputs.catalogs[l]?.[c.key] !== 'string' || !inputs.catalogs[l][c.key].trim())) {
      skipped.untranslated++
      continue
    }
    covered++
    const siteId = typeof c.siteId === 'string' ? c.siteId : null
    const site = siteId
      ? { siteId, c, ...autoSitePolicy(c, { siteId, deny, caution, english: english[c.key] }) }
      : { siteId: null, c, policy: 'search-only', reason: 'no_site_id' }
    const seenKey = `${parentId}\0${english[c.key]}`
    const group = groups.get(seenKey)
    if (group) {
      group.sites.push(site)
      // Two shared files with one label: the location lists every page either draws on.
      if (parentId === SHARED_PARENT) {
        const loc = locations.get(group.id)
        for (const p of placementsUnder()) {
          if (!loc.placements.some((q) => JSON.stringify(q) === JSON.stringify(p))) loc.placements.push(p)
        }
      }
      continue
    }
    const id = `auto:${parentId}:${c.key}`
    if (locations.has(id)) continue
    addLocation({
      id,
      kind: KINDS.has(c.kind) ? c.kind : 'button',
      tier: 'auto',
      conditions_unknown: true,
      label_key: c.key,
      // The parent's own proven gates (a preview flag, an enabled app) still
      // apply; the control's own are unknown, which conditions_unknown says.
      // A control only the app shell draws (the rail, the top bar) is on every
      // page: no route and no parent, like a registered shell control.
      placements: shell
        ? [{ surface_id: SHELL_SURFACE, route: '', parent_ids: [], entry_kind: 'content', requires: [] }]
        : placementsUnder(),
    }, 'auto')
    groups.set(seenKey, { id, sites: [site] })
  }
  // Each location's policy. Deny wins (a deny-listed location, or any site of
  // it denied); a guidable verdict (`point`, or `caution` for a destructive
  // site or a caution-listed location) takes exactly ONE site, itself
  // guidable, on placements with no prerequisite (a single-step plan cannot
  // walk a gate): one placement, or one per page for a shared control, whose
  // page decides at guide time which instance is the target (the only one
  // visible there). Everything else is search-only. Only a guidable location
  // gets a plan.
  const policy = { point: 0, caution: 0, 'search-only': 0, deny: 0 }
  const reasons = {}
  const stamps = []
  const destructive = []
  for (const { id, sites } of groups.values()) {
    const loc = locations.get(id)
    for (const s of sites) if (s.reason === 'sensitive_lint') destructive.push({ id, site: s.siteId, rel: s.c.rel, line: s.c.line, label: english[loc.label_key] })
    const shared = id.startsWith(`auto:${SHARED_PARENT}:`)
    let verdict
    let reason
    if (deny.has(id) || sites.some((s) => s.policy === 'deny')) {
      verdict = 'deny'
      reason = deny.has(id) ? 'denied' : sites.find((s) => s.policy === 'deny').reason
    } else if (sites.length !== 1) {
      verdict = 'search-only'
      reason = 'several_sites'
    } else if (!GUIDABLE_POLICIES.includes(sites[0].policy)) {
      verdict = 'search-only'
      reason = sites[0].reason
    } else if ((loc.placements.length !== 1 && !shared) || loc.placements.some((p) => p.requires.length !== 0)) {
      verdict = 'search-only'
      reason = 'placement_prerequisite'
    } else if (sites[0].policy === 'caution' || caution.has(id)) {
      verdict = 'caution'
      reason = sites[0].policy === 'caution' ? sites[0].reason : 'caution_listed'
    } else {
      verdict = 'point'
      reason = shared ? 'shared_unique_at_runtime' : 'audited_primitive'
    }
    loc.guide_policy = verdict
    policy[verdict]++
    reasons[reason] = (reasons[reason] ?? 0) + 1
    if (GUIDABLE_POLICIES.includes(verdict)) {
      loc.guide_plan = autoGuidePlan(loc, sites[0].siteId, { caution: verdict === 'caution' })
      const c = sites[0].c
      stamps.push({ rel: c.rel, tagEnd: c.tagEnd, openEnd: c.openEnd, tag: c.tag, siteId: sites[0].siteId })
    }
  }
  return {
    covered, skipped, entries: [...locations.values()].filter((l) => l.tier === 'auto').length,
    policy, reasons, stamps, destructive,
  }
}

const MAX_TERMS_PER_LOCALE = 16
const MAX_TERM_CHARS = 60

/** Approximates the search's normalization (NFKC, casefold, punctuation as space). */
function foldTerm(text) {
  return text.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{M}\p{N}]+/gu, ' ').trim()
}

/**
 * One location's search terms, validated and in index locale order, or null.
 * Every problem is an error: an unknown locale, a non-list, a blank, overlong
 * or repeated term, and a term that only repeats a label the index already
 * searches in that locale.
 */
function validateTerms(id, terms, origin, loc, inputs, errors) {
  const where = `${origin} '${id}'`
  if (!terms || typeof terms !== 'object' || Array.isArray(terms)) {
    errors.push(`${where}: terms must be an object of locale -> list`)
    return null
  }
  const out = {}
  for (const locale of Object.keys(terms)) {
    if (!inputs.locales.includes(locale)) errors.push(`${where}: unknown locale '${locale}' (shipped: ${inputs.locales.join(', ')})`)
  }
  for (const locale of inputs.locales) {
    const list = terms[locale]
    if (list === undefined) continue
    if (!Array.isArray(list) || list.length === 0) {
      errors.push(`${where}: terms.${locale} must be a nonempty list`)
      continue
    }
    if (list.length > MAX_TERMS_PER_LOCALE) errors.push(`${where}: terms.${locale} has ${list.length} terms; at most ${MAX_TERMS_PER_LOCALE}`)
    const cat = inputs.catalogs[locale] ?? {}
    const labelForms = new Set(
      [loc.label_key, ...(loc.alias_keys ?? [])]
        .map((k) => (k.startsWith(LITERAL_PREFIX) ? k.slice(LITERAL_PREFIX.length) : cat[k]))
        .filter((v) => typeof v === 'string')
        .map((v) => foldTerm(v.replaceAll('{{productName}}', inputs.productName))),
    )
    const seen = new Set()
    const kept = []
    for (const term of list) {
      if (typeof term !== 'string' || !foldTerm(term)) {
        errors.push(`${where}: terms.${locale} has a blank or non-string term`)
        continue
      }
      if (term.trim().length > MAX_TERM_CHARS) errors.push(`${where}: term '${term}' is longer than ${MAX_TERM_CHARS} characters`)
      const folded = foldTerm(term)
      if (seen.has(folded)) errors.push(`${where}: term '${term}' is listed twice for ${locale}`)
      else if (labelForms.has(folded)) errors.push(`${where}: term '${term}' only repeats the ${locale} label, which is searched already`)
      seen.add(folded)
      kept.push(term.trim())
    }
    out[locale] = kept
  }
  return Object.keys(out).length ? out : null
}

/**
 * The build-time auto tier, split out of an index built WITH auto candidates:
 * only its `tier: auto` locations and the labels they use. It names the
 * committed index it hangs off (`base_input_digest`): every auto placement's
 * path ends at a page or tab of that index, so find_ui refuses the artifact
 * (auto tier unavailable, committed index untouched) when it was built against
 * a different one. `coverage` is extra build-time reporting (core control
 * coverage), carried along so the shipped file says what it was built from.
 */
export function buildAutoArtifact(full, { baseInputDigest, baseBuildDigest = '', inputDigest, coverage = {} }) {
  const locations = full.locations.filter((l) => l.tier === 'auto')
  const keys = new Set(locations.map((l) => l.label_key))
  const labels = {}
  for (const [locale, table] of Object.entries(full.labels)) {
    labels[locale] = Object.fromEntries(Object.entries(table).filter(([k]) => keys.has(k)))
  }
  const policies = { point: 0, caution: 0, 'search-only': 0, deny: 0 }
  for (const l of locations) policies[l.guide_policy ?? 'search-only']++
  return {
    $comment: 'AUTO-GENERATED at build time by website/scripts/gen-ui-index.mjs --auto-out — DO NOT EDIT, never committed. '
      + 'The find_ui auto tier; read beside docs/ui-index.generated.json by kiro_crew/ui_index.py.',
    schema_version: SCHEMA_VERSION,
    artifact: 'auto',
    base_input_digest: baseInputDigest,
    input_digest: inputDigest,
    // The committed index's guide digest this tier's plans hang off, and the
    // tier's own (see autoBuildDigest): an auto `ui.show` guide carries the latter.
    base_build_digest: baseBuildDigest,
    build_digest: autoBuildDigest(baseBuildDigest, locations),
    coverage: {
      scope: AUTO_SCOPE,
      auto_controls: locations.length,
      auto_point: policies.point,
      auto_caution: policies.caution,
      auto_search_only: policies['search-only'],
      auto_denied: policies.deny,
      ...coverage,
    },
    locales: [...full.locales],
    locations,
    labels,
  }
}

/**
 * Stable bytes for the committed file and the `--check` comparison: one
 * location per line and one label per line, so a change reads as a small diff
 * without paying indentation on every nested field.
 */
/**
 * The browser's copy of every `ui.show` plan, as a TypeScript module
 * (`src/uiLocations/guidePlans.gen.ts`): the same plans the committed index
 * carries, keyed by location id, so the page and the gateway read one source.
 */
/**
 * Conditions that hold only while there is something for a control to act on
 * (a notification to clear or mark read). A control drawn only under one of
 * them is absent, not hidden, when there is nothing: the guide says so instead
 * of asking the person to open a menu it is not in.
 */
export const ONLY_WITH_ITEMS_CONDITIONS = Object.freeze(['has_notifications', 'has_unread_notifications'])

/** Registered locations every placement of which needs an ONLY_WITH_ITEMS_CONDITIONS condition. */
function guideOnlyWithItems(index) {
  const only = new Set(ONLY_WITH_ITEMS_CONDITIONS)
  return index.locations
    .filter((l) => l.tier !== 'auto' && l.placements?.length
      && l.placements.every((p) => (p.requires ?? []).some((q) => q.kind === 'condition' && only.has(q.id))))
    .map((l) => l.id)
    .sort()
}

function guideCautionLocations(index) {
  const ids = new Set()
  for (const l of index.locations) {
    if (l.guide_policy === 'caution') ids.add(l.id)
    for (const p of l.guide_plan?.placements ?? []) for (const s of p.steps) if (s.caution && s.location) ids.add(s.location)
  }
  return [...ids].sort()
}

export function serializeGuidePlans(index) {
  const plans = {}
  for (const l of index.locations) if (l.guide_plan) plans[l.id] = l.guide_plan
  return '// AUTO-GENERATED by website/scripts/gen-ui-index.mjs from the dashboard source — DO NOT EDIT.\n'
    + '// Regenerate with `npm run gen:ui`; the same plans as `guide_plan` in src/kiro_crew/docs/ui-index.generated.json.\n'
    + "import type { UiGuidePlan } from './types'\n\n"
    + '/** The `build_digest` of the packaged index this bundle was built with (see `guideBuildDigest`). */\n'
    + `export const GUIDE_BUILD_DIGEST = ${JSON.stringify(index.build_digest)}\n\n`
    + '/** Curated location ids: the only ids a live observation may name. */\n'
    + `export const GUIDE_OBSERVABLE_IDS: readonly string[] = ${JSON.stringify(guideObservableIds(index), null, 2)}\n\n`
    + '/** Every compiled reveal scope: id -> what kind of container it is and the control that opens it. */\n'
    + `export const GUIDE_REVEAL_SCOPES = ${JSON.stringify(index.reveal_scopes ?? {}, null, 2)} as const\n\n`
    + '/** A reveal scope id of this build: the only ids a `<GuideRevealScope>` owner may report. */\n'
    + 'export type GuideRevealScopeId = keyof typeof GUIDE_REVEAL_SCOPES\n\n'
    + '/** Locations whose guide policy is `caution` (a destructive control), and the sites their plans point at. */\n'
    + `export const GUIDE_CAUTION_LOCATIONS: readonly string[] = ${JSON.stringify(guideCautionLocations(index), null, 2)}\n\n`
    + '/** Locations drawn only while there is something for them to act on (`ONLY_WITH_ITEMS_CONDITIONS`): missing means nothing to act on yet. */\n'
    + `export const GUIDE_ONLY_WITH_ITEMS: readonly string[] = ${JSON.stringify(guideOnlyWithItems(index), null, 2)}\n\n`
    + `export const GUIDE_PLANS: Readonly<Record<string, UiGuidePlan>> = ${JSON.stringify(plans, null, 2)}\n`
}

export function serializeIndex(index) {
  const parts = []
  for (const [key, value] of Object.entries(index)) {
    const k = JSON.stringify(key)
    if (key === 'locations') {
      parts.push(`${k}: [\n${value.map((v) => JSON.stringify(v)).join(',\n')}\n]`)
    } else if (key === 'labels') {
      const locales = Object.entries(value).map(([loc, map]) => {
        const rows = Object.entries(map).map(([lk, lv]) => `${JSON.stringify(lk)}: ${JSON.stringify(lv)}`)
        return `${JSON.stringify(loc)}: {\n${rows.join(',\n')}\n}`
      })
      parts.push(`${k}: {\n${locales.join(',\n')}\n}`)
    } else {
      parts.push(`${k}: ${JSON.stringify(value)}`)
    }
  }
  return `{\n${parts.join(',\n')}\n}\n`
}
