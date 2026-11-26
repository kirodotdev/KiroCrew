/**
 * `permissions.browserRecipes` in words, the same on the trust prompt, the app page's
 * approval notice and its permissions card: each recipe's name and when it runs, its
 * site, then every step as a sentence with the selector set apart in monospace.
 */
import { Trans } from 'react-i18next'
import { i18nT } from '../../i18n/t'
import { fmtUnit } from '../../i18n/format'
import type { RecipeLine, RecipeStep } from './browserRecipes'

export interface BrowserRecipeItem {
  /** Unique per list: the grant entry, or the recipe's name. */
  key: string
  line: RecipeLine
  /** Declared but not approved yet: marked as held back. */
  staged?: boolean
  /** A watch recipe the owner also let run by itself (`browserRecipeWatch`). */
  watchOn?: boolean
}

function whenItRuns(line: RecipeLine, watchOn?: boolean): string {
  if (!line.watch) return i18nT('components.appstore.browserRecipeList.runs_when_asked')
  // Running by itself takes a second, separate yes on the app's page.
  return watchOn
    ? i18nT('components.appstore.browserRecipeList.runs_by_itself')
    : i18nT('components.appstore.browserRecipeList.runs_by_itself_if_allowed')
}

function StepText({ step }: { step: RecipeStep }) {
  // The selector is stored text, so it is the component's own child, never a `values` entry.
  const sel = <code data-testid="browser-recipe-selector" className="[overflow-wrap:anywhere] font-mono text-[11px] text-muted">{step.selector}</code>
  if (step.kind === 'click') return <Trans i18nKey="components.appstore.browserRecipeList.step_click" components={{ sel }} />
  return (
    <Trans
      i18nKey="components.appstore.browserRecipeList.step_wait"
      values={{ duration: fmtUnit(step.timeout, 'second', { unitDisplay: 'long' }) }}
      components={{ sel }}
    />
  )
}

export function BrowserRecipeList({ items, testIdPrefix }: { items: BrowserRecipeItem[]; testIdPrefix?: string }) {
  return (
    <ul className="flex flex-col gap-2">
      {items.map(({ key, line, staged, watchOn }) => (
        <li
          key={key}
          data-testid={testIdPrefix ? `${testIdPrefix}${line.name}` : undefined}
          className="flex min-w-0 flex-col gap-0.5 rounded border border-border bg-bg-elevated px-2 py-1.5 text-[12px] text-text"
        >
          <div className="flex flex-wrap items-baseline gap-x-1.5">
            <span className="font-medium">{line.name}</span>
            <span className="text-muted">· {whenItRuns(line, watchOn)}</span>
            {staged && <span className="text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
          </div>
          <div className="[overflow-wrap:anywhere] text-[11px] text-muted">{i18nT('components.appstore.browserRecipeList.on_site', { site: line.site })}</div>
          <ol className="ml-4 list-decimal">
            {line.steps.map((step, i) => (
              <li key={i} data-testid="browser-recipe-step"><StepText step={step} /></li>
            ))}
          </ol>
        </li>
      ))}
    </ul>
  )
}
