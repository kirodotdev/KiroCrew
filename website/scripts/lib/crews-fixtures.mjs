/**
 * Shared /api stub for the Crews-roster screenshot harnesses.
 *
 * Both `capture-crews-tab.mjs` and `capture-crews-list-modal.mjs` need the same
 * four endpoints to render a populated roster, and each had its own copy — which
 * the jscpd gate correctly flagged. The FIXTURES stay per-script (they are the
 * interesting, deliberately-different part); only the wiring lives here.
 */
import { json } from './stub-dashboard-api.mjs'

/**
 * Build a `stubDashboardApi({ extra })` handler for the Crews roster.
 *
 * @param {object} opts
 * @param {Array<object>} opts.crews         `/api/agents` roster
 * @param {string} [opts.defaultAgent]       which crew new sessions use
 * @param {Array<string>} [opts.workspaces]  workspace names; defaults to the
 *   distinct workspaces the crews point at, so a caller cannot hand the select a
 *   list that is missing the value a crew is bound to.
 * @param {Array<string>} [opts.memoryStores] as above, for memory stores
 * @param {Array<string>} [opts.installed]   agent-template names
 */
export function crewsApi({ crews, defaultAgent, workspaces, memoryStores, installed }) {
  const uniq = key => [...new Set(crews.map(c => c[key]).filter(Boolean))]
  const wsNames = workspaces ?? uniq('workspace')
  const msNames = memoryStores ?? uniq('memory_store')
  const templates = installed ?? uniq('kiro_agent')

  return async function handle(path, route) {
    if (path === '/api/agents') {
      return json(route, { agents: crews, default_agent: defaultAgent ?? crews[0]?.name ?? '' }), true
    }
    if (path === '/api/agents/installed') {
      return json(route, templates.map(name => ({ name }))), true
    }
    if (path === '/api/workspaces') {
      return json(route, { workspaces: wsNames.map(name => ({ name })) }), true
    }
    if (path === '/api/config/kirocrew') {
      return json(route, { memory_stores: Object.fromEntries(msNames.map(n => [n, {}])) }), true
    }
    return false
  }
}
