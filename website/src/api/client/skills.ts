/**
 * Prompts and skills: prompt (Agent SOP) CRUD, the skill list, project
 * trust, skill detail/tree/file and CRUD, the pending auto-skill queue, pin and
 * inject-on-trigger, the context budget, and skill discovery, preview and
 * install.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import { withDeadline } from '../../lib/withDeadline'
import type { ClientTransport } from './transport'

/** Deadline for `api.skills`. Measured on one host inside twenty minutes: 0.62s
 *  healthy, then 9.76s, 41.41s, 168.71s for a byte-identical payload. 15s clears
 *  the 9.76s case that still completed and cuts off the pathological ones.
 *  Rationale in the CR description. */
export const SKILLS_TIMEOUT_MS = 15_000

/** Static-validation verdict for a pending auto-skill candidate's bundled
 *  scripts, served on both the pending list entries and the detail payload
 *  (`/api/skills/-/pending[/{slug}]`). `ok: true` with an empty report when the
 *  candidate has no scripts; on failure `report` maps each offending file to
 *  its flagged constructs. The same report rides the 422
 *  `script_validation_failed` approve refusal. */
export interface SkillScriptValidation {
  ok: boolean
  report: Record<string, string[]>
}

export function createSkillsEndpoints({ get, post, put, del, j }: ClientTransport) {
  const library = {
    // Prompts (Agent SOPs)
    prompts: () => fetch(relocateRequestUrl('/api/prompts')).then(j),
    promptDetail: (name: string, scope?: 'global' | 'local') =>
      fetch(relocateRequestUrl('/api/prompts/' + name.split('/').map(encodeURIComponent).join('/')
        + (scope ? '?scope=' + scope : ''))).then(j),
    createPrompt: (name: string, content: string, scope: 'global' | 'local') =>
      post('/api/prompts', { name, content, scope }).then(j),
    updatePrompt: (name: string, scope: 'global' | 'local', content: string, baseHash: string) =>
      put('/api/prompts/' + encodeURIComponent(name) + '?scope=' + scope, { content, base_hash: baseHash }).then(j),
    deletePrompt: (name: string, scope: 'global' | 'local') =>
      del('/api/prompts/' + encodeURIComponent(name) + '?scope=' + scope).then(j),
    // Skills
    // sessionKey names the REAL chat slot so the server can resolve THIS chat's
    // project and include its `<project>/.kiro/skills`. Without it the shared
    // `dashboard:ui` placeholder makes the server fall back to "the one project
    // every slot shares", so workspace skills leak between chats on different
    // projects and vanish entirely when two chats disagree (#2457, #3551).
    // agent, when given, scopes the listing to that agent's own skill:// mapping;
    // an agent with no explicit mapping keeps the unfiltered listing. When the
    // mapping IS applied the server answers with the envelope
    // {skills, agent_scoped: true, agent} instead of the bare array, so the
    // picker can cue the scope (and tell "nothing mapped" from "nothing exists").
    // Consume through lib/skillsPayload.ts unwrapSkills() rather than assuming an array.
    // Bounded HERE, not per initiator: react-query dedupes on the key, so the
    // weakest initiator would otherwise decide whether the promise is bounded.
    skills: (sessionKey?: string, agent?: string, signal?: AbortSignal) =>
      withDeadline(SKILLS_TIMEOUT_MS, signal, s =>
        get('/api/skills' + (agent ? '?agent=' + encodeURIComponent(agent) : ''),
            sessionKey, s).then(j)),
    /** Project-skills trust: this chat's grant state plus every stored grant. */
    skillTrust: (sessionKey?: string) => get('/api/skills/-/trust', sessionKey).then(j),
    /** Grant trust to THIS chat's project. The server takes the directory from
     *  the slot, not from us — a caller-supplied path would let any caller
     *  consent for a directory the operator never opened. */
    // expectedKey is the canonical identity returned by the consent snapshot. It
    // is a confirmation, not a selector: the server still derives the directory
    // from the requesting slot and refuses when the current key differs.
    grantSkillTrust: (sessionKey: string | undefined, expectedKey: string) =>
      post('/api/skills/-/trust', { expected_key: expectedKey }, sessionKey).then(j),
    /** Revoke a grant. `path` is optional — omitted revokes this chat's project. */
    revokeSkillTrust: (path?: string, sessionKey?: string) =>
      del('/api/skills/-/trust' + (path ? '?path=' + encodeURIComponent(path) : ''),
          undefined, sessionKey).then(j),
    skill: (name: string) => fetch(relocateRequestUrl('/api/skills/' + name.split('/').map(encodeURIComponent).join('/'))).then(j),
    /** List the file tree under a skill's directory.  The ``/-/`` separator
     *  disambiguates from a nested skill whose last segment is ``tree``. */
    skillTree: (name: string) => fetch(relocateRequestUrl('/api/skills/' + name.split('/').map(encodeURIComponent).join('/') + '/-/tree')).then(j),
    /** Read a single file inside a skill's directory by relative path. */
    skillFile: (name: string, relPath: string) =>
      fetch(relocateRequestUrl('/api/skills/' + name.split('/').map(encodeURIComponent).join('/') +
            '/-/file?path=' + encodeURIComponent(relPath))).then(j),
    createSkill: (name: string, content: string) => post('/api/skills', { name, content }).then(j),
    updateSkill: (name: string, content: string) => put('/api/skills/' + name.split('/').map(encodeURIComponent).join('/'), { content }).then(j),
    deleteSkill: (name: string) => del('/api/skills/' + name.split('/').map(encodeURIComponent).join('/')).then(j),
  }

  const curation = {
    // Auto-skill pending queue + lifecycle pin
    skillsPending: () => fetch(relocateRequestUrl('/api/skills/-/pending')).then(j),
    /** Detail payload carries `script_validation` (`SkillScriptValidation`) so the
     *  review card can warn BEFORE the click that Approve cannot succeed as-is. */
    skillPendingDetail: (slug: string) => fetch(relocateRequestUrl('/api/skills/-/pending/' + encodeURIComponent(slug))).then(j),
    /** Throws ApiError on refusal: 404 `pending_skill_not_found`, 409
     *  `live_skill_exists`, 422 `script_validation_failed` (body carries a
     *  `report` of `{file: [findings]}`), 409 `pending_approval_refused`. */
    approvePendingSkill: (slug: string) => post('/api/skills/-/pending/' + encodeURIComponent(slug) + '/approve', {}).then(j),
    dismissPendingSkill: (slug: string) => post('/api/skills/-/pending/' + encodeURIComponent(slug) + '/dismiss', {}).then(j),
    dismissAllPendingSkills: (slugs: string[]) => post('/api/skills/-/pending/-/dismiss-all', { slugs }).then(j),
    pinSkill: (name: string, pinned: boolean) => post('/api/skills/-/pin', { name, pinned }).then(j),
    /** Opt a skill in/out of full-body injection when its triggers match.
     *  `inject: false` reduces the skill to a one-line pointer on a match. */
    setSkillInjectOnTrigger: (name: string, inject: boolean) =>
      post('/api/skills/-/inject-on-trigger', { name, inject }).then(j),
    /** Context budget: cost data for the skill control plane. */
    skillsBudget: () => get('/api/skills/-/budget').then(j) as Promise<import('../../types').SkillBudgetResponse>,
    /** Multi-provider skill discovery (skills.sh, etc.) */
    discoverSkills: (query: string, opts?: { provider?: string; limit?: number }) =>
      get(`/api/skills/-/discover?q=${encodeURIComponent(query)}${opts?.provider ? `&provider=${opts.provider}` : ''}${opts?.limit ? `&limit=${opts.limit}` : ''}`).then(j) as Promise<import('../../types').DiscoverSkillsResponse>,
    /** Preview a skill's description, full SKILL.md, and bundle manifest before installing */
    previewDiscoveredSkill: (provider: string, id: string) =>
      get(`/api/skills/-/discover/preview?provider=${encodeURIComponent(provider)}&id=${encodeURIComponent(id)}`).then(j) as Promise<import('../../types').DiscoverSkillPreview>,
    /** Install a skill from a provider by ID. Throws ApiError(409) when already installed and overwrite is not set. */
    installDiscoveredSkill: (provider: string, skillId: string, opts?: { name?: string; overwrite?: boolean }) =>
      post('/api/skills/-/discover/install', { provider, skill_id: skillId, name: opts?.name, overwrite: opts?.overwrite }).then(j) as Promise<import('../../types').DiscoverInstallResult>,
  }

  return { library, curation }
}
