/**
 * Shared fixtures for the Notes app's screenshot harnesses.
 *
 * Every md-notebook capture script needs the same scaffolding before it can
 * photograph anything: a vault descriptor, a notes list, a note document, an
 * API stub for the app's OWN backend, and a clip that frames the note pane.
 * Keeping one copy here is what lets each harness hold only the part that is
 * actually about its feature — the note content and the frames it shoots.
 *
 * The app's backend answers under `/apps/md-notebook/api/**`, NOT `/api/**`,
 * so `stubDashboardApi` alone leaves every one of these calls unanswered.
 */
import { json } from './stub-dashboard-api.mjs'

export const MDNB_VAULT_ID = 'local-notes'

/** A local, writable, single-vault setup — the shape with the fewest banners. */
export const MDNB_VAULT = {
  id: MDNB_VAULT_ID,
  name: 'My Notes',
  repo: '',
  branch: 'main',
  localPath: '/Users/demo/notes',
  readOnly: false,
  external: true,
  localOnly: true,
  knowledge: false,
  knowledgeSourceId: null,
}

/**
 * Every feature flag the UI probes on `/health`. One missing entry puts a
 * stale-backend banner over the surface the harness is trying to photograph.
 */
const MDNB_FEATURES = [
  'trash', 'move', 'createdAt', 'attach', 'changes', 'saveGuard',
  'forget', 'pat', 'newNote', 'duplicate', 'localOnly', 'autoCommit',
  'trashOpen', 'knowledge', 'pickFolder',
]

/**
 * The sidebar's note list: the harness's own note first, then two fillers so
 * the tree does not read as an almost-empty vault.
 */
export function mdnbNotesList(path, title) {
  return [
    { path, title, modifiedAt: Date.now(), syncStatus: 'synced' },
    { path: 'meeting-notes.md', title: 'Meeting Notes', modifiedAt: Date.now() - 3.6e6, syncStatus: 'synced' },
    { path: 'todo.md', title: 'TODO', modifiedAt: Date.now() - 7.2e6, syncStatus: 'synced' },
  ]
}

/** The opened note, with the empty metadata the preview is happy to render. */
export function mdnbNoteDoc(path, content) {
  return {
    path,
    content,
    mtime: Date.now(),
    meta: { frontmatter: {}, tags: [], links: [] },
    backlinks: [],
  }
}

/**
 * Build the `extra` handler `stubDashboardApi` delegates to, answering the
 * Notes app's own routes from the given fixtures.
 */
export function mdnbApiStub({ vault = MDNB_VAULT, notes, doc }) {
  return async function mdnbApi(path, route) {
    if (!path.startsWith('/apps/md-notebook/api/')) return false
    const appPath = path.slice('/apps/md-notebook/api'.length)

    if (appPath === '/health') {
      return json(route, { ok: true, features: MDNB_FEATURES }), true
    }
    if (appPath === '/vaults') {
      return json(route, { vaults: [vault], hasPat: false, hasGhAuth: false }), true
    }
    if (appPath.startsWith('/notes')) return json(route, { notes }), true
    if (appPath.startsWith('/note') && !appPath.startsWith('/note/')) return json(route, doc), true
    if (appPath.startsWith('/changes')) return json(route, { rev: 1, changed: [], watching: true }), true
    if (appPath.startsWith('/search')) return json(route, { results: [] }), true
    return json(route, {}), true
  }
}

/** Clip covering the note pane, so the sidebar does not dominate the frame. */
export async function notePaneClip(page) {
  return page.evaluate(() => {
    const heading = document.querySelector('h1')
    let el = heading?.parentElement
    while (el && el !== document.body) {
      const s = getComputedStyle(el)
      if (s.overflowY === 'auto' || s.overflow === 'auto') break
      el = el.parentElement
    }
    const r = (el && el !== document.body ? el : document.body).getBoundingClientRect()
    return {
      x: Math.max(0, Math.round(r.left)),
      y: 0,
      width: Math.round(Math.min(r.width, window.innerWidth - Math.max(0, r.left))),
      height: Math.min(Math.round(r.height), window.innerHeight),
    }
  })
}
