#!/usr/bin/env node
/**
 * discover-routes.mjs — find the UI routes in a code package.
 *
 * File-based frameworks are listed directly. Programmatic routing is only
 * scanned when a real router is a dependency, and only inside source dirs
 * (never node_modules / build output / this tool's own scripts). If there are
 * no crawlable routes, it says so plainly instead of inventing one.
 *
 * Usage: node discover-routes.mjs <project-dir>
 * Prints JSON: { framework, routing, routes:[{path,source}], needsConfirmation, hostPage?, notes }
 */
import { existsSync, readdirSync, statSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'

const root = process.argv[2] || process.cwd()
const notes = []

// Dirs we never walk: deps, build output, VCS, this tool's own scripts, tests.
const IGNORE = /^(node_modules|dist|build|out|coverage|\.next|\.nuxt|\.svelte-kit|\.git|\.cache|scripts|__pycache__|tests?|__tests__|e2e|\.turbo|\.vercel)$/

function findDir(...cands) { return cands.map(c => join(root, c)).find(existsSync) }
function read(p) { try { return readFileSync(p, 'utf8') } catch { return '' } }
function pkg() { try { return JSON.parse(read(join(root, 'package.json')) || '{}') } catch { return {} } }
function hasRouterDep(deps) {
  return !!(deps['react-router'] || deps['react-router-dom'] || deps['vue-router'] ||
    deps['@tanstack/react-router'] || deps['@reach/router'])
}

// Walk a file-based routing dir (Next app/, pages/, Remix/SvelteKit/Nuxt routes/).
function walkFileRoutes(dir, base = '', mode = 'app') {
  const out = []
  for (const name of readdirSync(dir)) {
    if (name.startsWith('.') || IGNORE.test(name)) continue
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      const seg = name.replace(/^\((.*)\)$/, '').replace(/^\[\.\.\.(.+)\]$/, '*').replace(/^\[(.+)\]$/, ':$1')
      out.push(...walkFileRoutes(full, seg ? `${base}/${seg}` : base, mode))
    } else {
      const isPage = mode === 'app'
        ? /^page\.(t|j)sx?$/.test(name)
        : /\.(t|j)sx?$/.test(name) && !/^_/.test(name) && !/\.(test|spec)\./.test(name)
      if (isPage) {
        let p = base
        if (mode !== 'app') {
          const f = name.replace(/\.(t|j)sx?$/, '').replace(/^index$/, '')
          p = `${base}/${f}`.replace(/\/index$/, '')
        }
        out.push({ path: p === '' ? '/' : p.replace(/\/+/g, '/'), source: relative(root, full) })
      }
    }
  }
  return out
}

// Programmatic routes: ONLY in files that actually use a router, path must start with "/".
function scanProgrammatic(dir, depth = 0) {
  const routes = []
  if (depth > 5 || !existsSync(dir)) return routes
  for (const n of readdirSync(dir)) {
    if (n.startsWith('.') || IGNORE.test(n)) continue
    const f = join(dir, n)
    const s = statSync(f)
    if (s.isDirectory()) { routes.push(...scanProgrammatic(f, depth + 1)); continue }
    if (!/\.(t|j)sx?$/.test(n)) continue // real source only — skip .mjs tooling
    const src = read(f)
    if (!/react-router|vue-router|createBrowserRouter|@tanstack\/react-router|<Routes\b|<Route\b/.test(src)) continue
    const re = /<Route\b[^>]*\bpath=['"`]([^'"`]+)['"`]|\bpath:\s*['"`](\/[^'"`]*)['"`]/g
    let m
    while ((m = re.exec(src))) {
      const p = m[1] || m[2]
      if (p && p.startsWith('/')) routes.push({ path: p, source: relative(root, f) })
    }
  }
  return routes
}

function detect() {
  const p = pkg()
  const deps = { ...p.dependencies, ...p.devDependencies }

  // KiroCrew app with an embedded UI — no routes of its own.
  const ajRaw = read(join(root, 'app.json'))
  if (ajRaw) {
    try {
      const aj = JSON.parse(ajRaw)
      if (aj.ui || aj.mcpServers || aj.agents) {
        const hostPage = aj.ui?.pages?.[0]?.route || null
        notes.push('This is a KiroCrew app with an embedded UI — it has no routes to crawl by itself. Run the whole KiroCrew dashboard and open its page' + (hostPage ? ` (${hostPage})` : '') + ', or give a screenshot.')
        return { framework: 'KiroCrew app (embedded UI)', routing: 'host-embedded', routes: [], hostPage }
      }
    } catch { /* not valid json — ignore */ }
  }

  // Electron desktop app — not a web server.
  if (deps.electron || /electron/i.test(p.main || '')) {
    notes.push('Electron desktop app — not a web server. Screenshot the running app instead.')
    return { framework: 'Electron app', routing: 'desktop', routes: [] }
  }

  // Next.js app router
  let d = findDir('app', 'src/app')
  if (d && (deps.next || existsSync(join(root, 'next.config.js')) || existsSync(join(root, 'next.config.ts'))))
    return { framework: 'Next.js', routing: 'file-based (app router)', routes: walkFileRoutes(d, '', 'app') }
  // Next.js pages router
  d = findDir('pages', 'src/pages')
  if (d && deps.next) return { framework: 'Next.js', routing: 'file-based (pages router)', routes: walkFileRoutes(d, '', 'pages') }
  // Remix / SvelteKit / Nuxt
  d = findDir('app/routes', 'src/routes')
  if (d) {
    const fw = deps['@remix-run/react'] ? 'Remix' : deps['@sveltejs/kit'] ? 'SvelteKit' : deps.nuxt ? 'Nuxt' : 'file-based framework'
    return { framework: fw, routing: 'file-based (routes/)', routes: walkFileRoutes(d, '', 'pages') }
  }

  // Programmatic SPA — only when a router is actually a dependency.
  if (hasRouterDep(deps)) {
    const routes = scanProgrammatic(findDir('src') || root)
    if (routes.length) {
      notes.push('Programmatic routing — route list is best-effort. Confirm with the user before capturing.')
      return { framework: deps.vue ? 'Vue' : 'React (SPA)', routing: 'programmatic', routes, needsConfirmation: true }
    }
  }

  // Static HTML site
  const htmls = existsSync(root) ? readdirSync(root).filter(n => n.endsWith('.html')) : []
  if (htmls.length) return { framework: 'Static HTML', routing: 'files', routes: htmls.map(h => ({ path: '/' + h, source: h })) }

  notes.push('No crawlable web routes found. If this is a component library or an embedded widget, render individual components (e.g. Storybook) or provide a screenshot.')
  return { framework: 'not a standalone web app', routing: 'none', routes: [] }
}

const res = detect()
res.needsConfirmation = !!res.needsConfirmation
res.notes = notes
const seen = new Set()
res.routes = (res.routes || []).filter(r => !seen.has(r.path) && seen.add(r.path)).sort((a, b) => a.path.localeCompare(b.path))
console.log(JSON.stringify(res, null, 2))
console.error(`discover-routes: ${res.framework} · ${res.routing} · ${res.routes.length} routes${res.needsConfirmation ? ' (confirm with user)' : ''}`)
