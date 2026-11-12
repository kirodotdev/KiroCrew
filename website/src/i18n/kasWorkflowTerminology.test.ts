/**
 * Copy guard: the Kiro Agent workflows surfaces call the thing a person
 * creates, edits and deletes a "workflow" everywhere a person can see it.
 *
 * Internally these are "recipes" (the key names keep that word, and so do the
 * hooks/API), but the picker, the panel and the section already label them
 * "Workflow"/"Workflows" — while the create / edit / delete / refresh strings
 * had leaked the internal word as "recipe". Every one of the eleven shipped
 * translations had ALREADY rendered them as "workflow", so the English source
 * was the lone outlier. This pins the fix so the word cannot creep back in.
 */
import { describe, it, expect } from 'vitest'

import { EN_TRANSLATION } from './enCatalog'

const kas = (EN_TRANSLATION as { apps: { workflows: { kas: Record<string, string> } } }).apps.workflows.kas

/** A dotted-path string leaf from the merged English catalog. */
function enString(path: string): string {
  let node: unknown = EN_TRANSLATION
  for (const part of path.split('.')) {
    node = (node as Record<string, unknown>)?.[part]
  }
  return typeof node === 'string' ? node : ''
}

/**
 * Copy guard: one name for the feature — "Kiro Workflows" — everywhere
 * the copy NAMES it. "Kiro Agent" is the engine/backend and stays only where it
 * means that (the switch label "… when Kiro Agent Server is configured",
 * "the backend is Kiro Agent", version-not-supported messages). These are the
 * user-facing strings that used to brand the feature "Kiro Agent workflow(s)" /
 * "Kiro Agent's own workflows"; this pins the single name so the old form
 * cannot creep back.
 */
const FEATURE_NAME_KEYS = [
  'apps.workflows.kas.empty_hint',
  'components.slashCommandMenu.desc_workflow_run',
  'components.slashCommandMenu.desc_workflow_resume',
  'components.slashCommandMenu.desc_workflow_status',
  'components.slashCommandMenu.desc_workflow_cancel',
  'pages.developer.featurePreviewsTab.kas_workflows_desc',
] as const

/** The old feature-name forms, in any case: "Kiro Agent workflow(s)" and
 *  "Kiro Agent's own workflow(s)". The engine sense ("Kiro Agent Server",
 *  "Kiro Agent's own engine") does not match. */
const OLD_FEATURE_NAME = /Kiro Agent(?:'s own)?\s+workflow/i

describe('KAS workflows — one feature name "Kiro Workflows"', () => {
  it('never brands the feature "Kiro Agent workflow" in user-facing copy', () => {
    const leaks = FEATURE_NAME_KEYS.filter(k => OLD_FEATURE_NAME.test(enString(k)))
    expect(leaks).toEqual([])
  })

  it('names the feature "Kiro Workflow(s)" in those strings', () => {
    const missing = FEATURE_NAME_KEYS.filter(k => !/Kiro Workflow/.test(enString(k)))
    expect(missing).toEqual([])
  })

  it('keeps "Kiro Agent" for the engine in the switch label', () => {
    // The switch deliberately names both: the feature it turns on (Kiro
    // Workflows) and the backend it needs (Kiro Agent Server).
    expect(enString('pages.developer.featurePreviewsTab.kas_workflows')).toMatch(/Kiro Workflows/)
    expect(enString('pages.developer.featurePreviewsTab.kas_workflows')).toMatch(/Kiro Agent Server/)
  })
})

describe('KAS workflows — not_launchable copy (CX r8)', () => {
  it('drops the internal "launch source" jargon', () => {
    expect(kas.not_launchable).not.toMatch(/launch source/i)
  })

  it('says, in plain words, that there is no file to launch from', () => {
    expect(kas.not_launchable).toMatch(/no file to launch from/i)
    expect(kas.not_launchable).not.toMatch(/\brecipe\b/i)
  })
})

/** The user-facing strings for the workflow-definition CRUD flow. */
const USER_FACING_KEYS = [
  'recipe',
  'recipes',
  'choose_recipe',
  'not_launchable',
  'refresh_recipes',
  'new_recipe',
  'new_recipe_name',
  'create_recipe',
  'delete_recipe',
  'delete_recipe_title',
  'delete_recipe_body',
  'delete_recipe_confirm',
  'edit_recipe',
  'recipe_steps_unavailable',
] as const

describe('KAS workflows — user-facing terminology', () => {
  it('never shows the internal word "recipe" to a person', () => {
    const leaks = USER_FACING_KEYS.filter(k => /\brecipe\b/i.test(kas[k] ?? ''))
    expect(leaks).toEqual([])
  })

  it('names the concept "workflow" in the create and delete controls', () => {
    expect(kas.new_recipe).toMatch(/\bworkflow\b/i)
    expect(kas.create_recipe).toMatch(/\bworkflow\b/i)
    expect(kas.delete_recipe_title).toMatch(/\bworkflow\b/i)
    expect(kas.delete_recipe_confirm).toMatch(/\bworkflow\b/i)
  })
})
