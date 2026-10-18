/**
 * The find_ui auto tier's decisions: which ONE page (and, when proven, tab)
 * each source file is drawn on, from the module graph alone, and what each
 * unregistered render site is called and how a guide may treat it.
 *
 * It holds the module edges and region uses a host file proves (`scanImports`,
 * `classifyUses`, the Route element and tab-panel spans), the page map built
 * from them (`buildPageMap`, `makeRouteLabel`), a candidate's location kind,
 * the render-site ids (`autoSiteIds`), and the guide policy vocabulary with the
 * per-site verdict (`autoSitePolicy`). `SHELL_SURFACE` lives here as the
 * lowest module that reads it. A new page-map or per-site rule goes here; how
 * sites group into auto locations and each location's verdict
 * (`buildAutoLocations`), and the plans, digests and stamp manifest built from
 * them, are in `ui-index.mjs` and `ui-index-guide.mjs`.
 *
 * `ui-index.mjs` re-exports the public names, and callers import them from
 * there. Of the index modules, this one imports only `ui-index-source.mjs`.
 */

import { posix as posixPath } from 'node:path'

import ts from 'typescript'

import { findAttr, matchRoute, staticAttrString, tagName, unwrapExpr } from './ui-index-source.mjs'

/**
 * The dashboard shell (App.tsx's top bar): drawn over every page, so a
 * placement on it has no parent and no route. Emitted as `surface_id: 'shell'`
 * with `route: ''` and an empty path; find_ui reports it as on every page.
 */
export const SHELL_SURFACE = 'shell'

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
