/**
 * Goldens for the wave-3 additions to the `schedule`, `artifacts` and
 * `capabilities` areas (Schedule's job controls, the Artifacts toolbar extras,
 * Customize > Skills and the MCP probe): the exact parent chain, entry, route,
 * label key and requirements each location carries in the generated index, and
 * the one render site its marker sits on, in the committed index.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Req { kind: string; value?: string; location?: string; when?: string; id?: string; flag?: string }
interface Placement { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: Req[] }
interface Loc { id: string; kind: string; label_key: string; alias_keys?: string[]; placements: Placement[] }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as { locations: Loc[] }
const byId = new Map(index.locations.map(l => [l.id, l]))
const src = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf-8')
const SCHEDULE_SRC = src('../pages/SchedulePage.tsx')
const ARTIFACTS_SRC = src('../pages/ArtifactsPage.tsx')
const SKILLS_SRC = src('../pages/overview/SkillsTab.tsx')
const MCP_SRC = src('../pages/overview/McpTab.tsx')

const cond = (...ids: string[]): Req[] => ids.map(id => ({ kind: 'condition', id }))

type Expect = {
  kind: string; labelKey: string; aliasKeys?: string[]; source: string
  placement: { surface: string; route: string; parents: string[]; entry: string; requires: Req[] }
}
const schedule = (entry: string, requires: Req[]) => ({ surface: 'schedule', route: '/schedule', parents: ['page.schedule'], entry, requires })
const skills = { surface: 'capabilities', route: '/capabilities?tab=skills', parents: ['page.capabilities', 'tab.capabilities.skills'], entry: 'toolbar', requires: [] }

const EXPECTED: Record<string, Expect> = {
  'schedule.templates': {
    kind: 'button', labelKey: 'pages.schedulePage.browse_all_templates', source: SCHEDULE_SRC,
    placement: schedule('content', cond('no_schedules')),
  },
  'schedule.new-folder': {
    kind: 'button', labelKey: 'pages.schedulePage.cronFolders.new_folder', source: SCHEDULE_SRC,
    placement: schedule('toolbar', cond('has_schedules', 'schedule_list_view')),
  },
  'schedule.select-all': {
    kind: 'toggle', labelKey: 'pages.schedulePage.select_all_jobs', source: SCHEDULE_SRC,
    placement: schedule('content', cond('has_schedules', 'schedule_list_view')),
  },
  'schedule.run-now': {
    kind: 'button', labelKey: 'pages.schedulePage.run_now', source: SCHEDULE_SRC,
    placement: schedule('content', cond('job_open', 'job_not_running')),
  },
  'schedule.cancel-run': {
    kind: 'button', labelKey: 'pages.schedulePage.cancel_run', source: SCHEDULE_SRC,
    placement: schedule('content', cond('job_open', 'job_running')),
  },
  'schedule.delete': {
    kind: 'button', labelKey: 'pages.schedulePage.delete', source: SCHEDULE_SRC,
    placement: schedule('content', cond('job_open')),
  },
  'schedule.secret-approve': {
    kind: 'button', labelKey: 'pages.schedulePage.secrets_approve', source: SCHEDULE_SRC,
    placement: schedule('content', cond('job_open', 'job_details_tab', 'job_secret_request_pending')),
  },
  'artifacts.starred': {
    kind: 'toggle', labelKey: 'pages.artifactsPage.starred', aliasKeys: ['pages.artifactsPage.filter_starred'], source: ARTIFACTS_SRC,
    placement: { surface: 'artifacts', route: '/artifacts', parents: ['page.artifacts'], entry: 'toolbar', requires: [] },
  },
  'artifacts.new-folder': {
    kind: 'button', labelKey: 'pages.artifactsPage.new_folder',
    aliasKeys: ['pages.artifactsPage.create_a_folder_to_organize_your_artifacts'], source: ARTIFACTS_SRC,
    placement: { surface: 'artifacts', route: '/artifacts', parents: ['page.artifacts'], entry: 'toolbar', requires: [{ kind: 'viewport', value: 'desktop' }] },
  },
  'artifacts.deploy': {
    kind: 'button', labelKey: 'pages.artifactsPage.artifact_deploy',
    aliasKeys: ['pages.artifactsPage.artifact_deploy_aws_profiles_and_published_sites'], source: ARTIFACTS_SRC,
    placement: {
      surface: 'artifacts', route: '/artifacts', parents: ['page.artifacts'], entry: 'toolbar',
      requires: [
        { kind: 'viewport', value: 'desktop' },
        { kind: 'preview_flag', flag: 'mc-preview-artifact-deploy', location: 'setting:developer.artifact-deploy' },
        ...cond('cloud_deploy_available'),
      ],
    },
  },
  'skills.create': { kind: 'button', labelKey: 'pages.overview.skillsTab.create_new_skill', source: SKILLS_SRC, placement: skills },
  'skills.add': { kind: 'button', labelKey: 'pages.overview.skillsTab.add_skill', source: SKILLS_SRC, placement: skills },
  'skills.refresh': { kind: 'button', labelKey: 'pages.overview.skillsTab.refresh_skills', source: SKILLS_SRC, placement: skills },
  'mcp.probe': {
    kind: 'button', labelKey: 'pages.overview.mcpTab.probe_mcp_servers', source: MCP_SRC,
    placement: {
      surface: 'capabilities', route: '/capabilities?tab=mcp',
      parents: ['page.capabilities', 'tab.capabilities.mcp', 'connections.mcp-servers-tab'], entry: 'toolbar', requires: [],
    },
  },
}

describe('schedule, artifacts and capabilities wave-3 locations', () => {
  for (const [id, want] of Object.entries(EXPECTED)) {
    it(`${id}: one placement with its exact chain, route, label and requirements`, () => {
      const loc = byId.get(id)
      expect(loc, `${id} is not in the index`).toBeDefined()
      expect(loc!.kind).toBe(want.kind)
      expect(loc!.label_key).toBe(want.labelKey)
      expect(loc!.alias_keys ?? []).toEqual(want.aliasKeys ?? [])
      expect(loc!.placements).toHaveLength(1)
      const [p] = loc!.placements
      expect(p.surface_id).toBe(want.placement.surface)
      expect(p.route).toBe(want.placement.route)
      expect(p.parent_ids).toEqual(want.placement.parents)
      expect(p.entry_kind).toBe(want.placement.entry)
      expect(p.requires).toEqual(want.placement.requires)
    })

    it(`${id}: is marked at exactly one render site`, () => {
      const marker = `uiLocation('${id}'`
      expect(want.source.split(marker).length - 1).toBe(1)
    })
  }

  it('never binds the secret approval to a guide action', () => {
    expect(byId.get('schedule.secret-approve')).not.toHaveProperty('guide_ref')
    // The control: the field is real, so the assertion above can fail.
    expect(byId.get('mcp.add-custom')).toHaveProperty('guide_ref')
  })

  it('keeps the wave-2 entries of these areas unchanged', () => {
    expect(byId.get('schedule.create-first')!.placements[0].requires).toEqual(cond('no_schedules'))
    expect(byId.get('schedule.add-job')!.placements[0].requires).toEqual(cond('has_schedules'))
    expect(byId.get('artifacts.import')!.placements[0].parent_ids).toEqual(['page.artifacts', 'artifacts.add-menu'])
    expect(byId.get('mcp.add-custom')!.placements[0].parent_ids).toEqual(['page.capabilities', 'tab.capabilities.mcp', 'connections.mcp-servers-tab'])
  })
})
