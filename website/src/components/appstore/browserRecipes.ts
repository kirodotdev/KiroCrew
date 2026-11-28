/**
 * `permissions.browserRecipes`: the fixed clicks Crew runs on an app's own browser tab
 * (`apps/browser_recipes.py`). The trust prompt and the app's page show each one as
 * the gateway will run it: its name, its site, and every step with its selector
 * (`BrowserRecipeList.tsx` puts that into words).
 */

/** One step as the gateway runs it: a click, or a wait of up to `timeout` seconds. */
export type RecipeStep =
  | { kind: 'click'; selector: string }
  | { kind: 'wait'; selector: string; timeout: number }

export interface RecipeLine {
  name: string
  site: string
  watch: boolean
  steps: RecipeStep[]
}

function stepLine(step: unknown): RecipeStep | null {
  if (!step || typeof step !== 'object') return null
  const s = step as Record<string, unknown>
  if (typeof s.click === 'string') return { kind: 'click', selector: s.click }
  if (typeof s.waitFor === 'string') {
    return { kind: 'wait', selector: s.waitFor, timeout: typeof s.timeout === 'number' ? s.timeout : 5 }
  }
  return null
}

/** One recipe, raw from a manifest or parsed from a grant entry; `null` when it is not one. */
export function recipeLine(raw: unknown): RecipeLine | null {
  let value = raw
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { return null }
  }
  if (!value || typeof value !== 'object') return null
  const r = value as Record<string, unknown>
  if (typeof r.name !== 'string' || typeof r.site !== 'string' || !Array.isArray(r.steps)) return null
  const steps = r.steps.map(stepLine)
  if (steps.some(s => s === null)) return null
  return { name: r.name, site: r.site, watch: r.watch === true, steps: steps as RecipeStep[] }
}

/** The recipes of a raw `permissions.browserRecipes` value that read as recipes. */
export function recipeLines(raw: unknown): RecipeLine[] {
  return Array.isArray(raw) ? raw.map(recipeLine).filter((r): r is RecipeLine => r !== null) : []
}
