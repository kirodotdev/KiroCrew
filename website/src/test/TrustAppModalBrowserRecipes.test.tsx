/**
 * `permissions.browserRecipes` at the trust gate and on the app's page.
 *
 * The trust gate shows every recipe as it will run, in words: name and when it
 * runs, site, and each step as a sentence with its selector set apart, because the
 * owner approves what was shown. On the app's page a recipe
 * an update added or changed is held back and offered with the other staged grants.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

const enableApp = vi.hoisted(() => vi.fn())
const stopAppRecipeWatch = vi.hoisted(() => vi.fn())
vi.mock('../api/client', async importOriginal => {
  const real = await importOriginal<typeof import('../api/client')>()
  return { ...real, api: { ...real.api, enableApp, stopAppRecipeWatch } }
})

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { trustGrants } from '../components/appstore/trustGrants'
import { recipeLine, recipeLines } from '../components/appstore/browserRecipes'
import { stagedGrantsOf } from '../pages/appDetailPageStagedGrants'
import { BrowserRecipesBox, StagedBrowserRecipes } from '../pages/AppDetailGrantBoxes'
import type { AppInfo } from '../pages/appDetailPageTypes'

const K = 'components.appstore.trustAppModal'
const SITE = 'https://www.youtube.com/watch'
const PLAY = { name: 'play', site: SITE, steps: [{ waitFor: 'button.play', timeout: 5 }, { click: 'button.play' }] }
const SKIP = { name: 'skip_ad', site: SITE, watch: true, steps: [{ click: '.skip' }] }
/** The grant entry the gateway stores for SKIP (`browser_recipes.recipe_entry`). */
const SKIP_ENTRY = JSON.stringify({ name: 'skip_ad', site: SITE, steps: [{ click: '.skip' }], watch: true })

const BASE: TrustAppTarget = {
  name: 'tunes',
  displayName: 'Tunes',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/tunes',
  retryInstalls: true,
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

const L = 'components.appstore.browserRecipeList'

/** Each step of a rendered recipe: its sentence, and the selector it names. */
function steps(el: HTMLElement) {
  return within(el).getAllByTestId('browser-recipe-step').map(li => ({
    text: li.textContent,
    selector: within(li).getByTestId('browser-recipe-selector').textContent,
  }))
}

describe('a recipe as data', () => {
  it('reads every step with its selector and wait', () => {
    expect(recipeLine(PLAY)).toEqual({
      name: 'play',
      site: SITE,
      watch: false,
      steps: [{ kind: 'wait', selector: 'button.play', timeout: 5 }, { kind: 'click', selector: 'button.play' }],
    })
  })

  it('reads a stored grant entry the same as the manifest recipe', () => {
    expect(recipeLine(SKIP_ENTRY)).toEqual(recipeLine(SKIP))
  })

  it('drops anything that is not a recipe', () => {
    expect(recipeLines([PLAY, { name: 'x', site: SITE, steps: [{ evaluate: 'alert(1)' }] }, 'not json', null])).toHaveLength(1)
    expect(recipeLines('nope')).toEqual([])
  })
})

describe('browser recipes at the trust gate', () => {
  it('lists every recipe in words, with its note', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({ browserRecipes: [PLAY, SKIP] }) })
    const el = dialog.getByTestId('trust-browser-recipes')
    const box = within(el)
    expect(box.getByText(`${K}.browser_recipes_heading`)).toBeTruthy()
    expect(box.getByText('play')).toBeTruthy()
    expect(box.getByText(`· ${L}.runs_when_asked`)).toBeTruthy()
    expect(box.getByText('skip_ad')).toBeTruthy()
    // Installing gives no run-by-itself yes: that is a second, separate one on the app's page.
    expect(box.getByText(`· ${L}.runs_by_itself_if_allowed`)).toBeTruthy()
    expect(box.getAllByText(`${L}.on_site ${SITE}`)).toHaveLength(2)
    expect(steps(el)).toEqual([
      { text: 'Wait up to 5\u00A0seconds for the element matching button.play', selector: 'button.play' },
      { text: 'Click the element matching button.play', selector: 'button.play' },
      { text: 'Click the element matching .skip', selector: '.skip' },
    ])
    expect(box.getAllByTestId('browser-recipe-selector')[0].className).toContain('text-muted')
    expect(el.textContent).not.toMatch(/[{}]/)
    expect(box.getByText(`${K}.browser_recipes_note`)).toBeTruthy()
  })

  it('shows no box for an app that declares none', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({}) })
    expect(dialog.queryByTestId('trust-browser-recipes')).toBeNull()
  })
})

describe('browser recipes on the app page', () => {
  it('marks a held-back recipe in the permissions card', () => {
    render(<BrowserRecipesBox grant={{ approved: [JSON.stringify(PLAY)], staged: [SKIP_ENTRY] }} />)
    expect(screen.getByTestId('browser-recipe-skip_ad').textContent).toContain('pages.appDetailPage.grant_not_approved')
    expect(screen.getByTestId('browser-recipe-play').textContent).not.toContain('pages.appDetailPage.grant_not_approved')
    expect(steps(screen.getByTestId('browser-recipe-skip_ad'))).toEqual([{ text: 'Click the element matching .skip', selector: '.skip' }])
    expect(screen.getByTestId('browser-recipes').textContent).not.toMatch(/[{}]/)
  })

  it('shows a held-back recipe in the approval notice in the same words', () => {
    render(<StagedBrowserRecipes label="label" entries={[SKIP_ENTRY]} />)
    const el = screen.getByTestId('staged-browser-recipes')
    expect(within(el).getByText('skip_ad')).toBeTruthy()
    expect(within(el).getByText(`· ${L}.runs_by_itself_if_allowed`)).toBeTruthy()
    expect(steps(el)).toEqual([{ text: 'Click the element matching .skip', selector: '.skip' }])
    expect(el.textContent).not.toMatch(/[{}]/)
  })

  it('still shows a held-back entry that does not read as a recipe, since approving sends it', () => {
    render(<StagedBrowserRecipes label="label" entries={['not json']} />)
    expect(screen.getByTestId('staged-browser-recipes').textContent).toBe('not json')
  })

  it('offers a held-back recipe for approval, and it is not a first Kiro approval', () => {
    const app = {
      name: 'tunes',
      browserRecipesGrant: { approved: [], staged: [SKIP_ENTRY] },
      kiroOperationsGrant: { approved: [], staged: ['GetWidget'] },
    } as unknown as AppInfo
    const staged = stagedGrantsOf(app)
    expect(staged.stagedBrowserRecipes).toEqual([SKIP_ENTRY])
    expect(staged.hasStagedGrants).toBe(true)
    expect(staged.firstKiroApproval).toBe(false)
  })

  const SWITCH = 'pages.appDetailPage.browser_recipe_watch_switch skip_ad'
  const watchSwitch = () => screen.getByRole('switch', { name: SWITCH })

  it('turns the separate run-by-itself yes on with a labelled switch, sending only that', async () => {
    enableApp.mockReset()
    enableApp.mockResolvedValue({ ok: true })
    const onChanged = vi.fn()
    render(
      <BrowserRecipesBox
        grant={{ approved: [SKIP_ENTRY], staged: [], watchOn: [], watchOff: [SKIP_ENTRY], preview: true }}
        appName="tunes"
        enabled
        onChanged={onChanged}
      />,
    )
    expect(screen.getByTestId('browser-recipe-skip_ad').textContent).toContain(`${L}.runs_by_itself_if_allowed`)
    expect(watchSwitch().getAttribute('aria-checked')).toBe('false')
    fireEvent.click(watchSwitch())
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(enableApp).toHaveBeenCalledWith('tunes', false, { api: [], events: [], browserRecipeWatch: [SKIP_ENTRY] })
    expect(stopAppRecipeWatch).not.toHaveBeenCalled()
  })

  it.each([true, false])('turns only that yes off, the app on or off (%s)', async (enabled) => {
    stopAppRecipeWatch.mockReset()
    stopAppRecipeWatch.mockResolvedValue({ ok: true, removed: true })
    enableApp.mockReset()
    render(<BrowserRecipesBox grant={{ approved: [SKIP_ENTRY], staged: [], watchOn: [SKIP_ENTRY], watchOff: [] }} appName="tunes" enabled={enabled} />)
    expect(screen.getByTestId('browser-recipe-skip_ad').textContent).toContain(`${L}.runs_by_itself`)
    expect(watchSwitch().getAttribute('aria-checked')).toBe('true')
    fireEvent.click(watchSwitch())
    await waitFor(() => expect(stopAppRecipeWatch).toHaveBeenCalledWith('tunes', SKIP_ENTRY))
    expect(enableApp).not.toHaveBeenCalled()
  })

  it('shows a refused run-by-itself yes as an error notice', async () => {
    enableApp.mockReset()
    enableApp.mockRejectedValue(new Error('the gateway said no'))
    render(<BrowserRecipesBox grant={{ approved: [SKIP_ENTRY], staged: [], watchOff: [SKIP_ENTRY] }} appName="tunes" enabled />)
    fireEvent.click(watchSwitch())
    expect(await screen.findByText('the gateway said no')).toBeTruthy()
  })

  it('offers no switch before the recipe is approved, and no turning on while the app is off', () => {
    const { rerender } = render(<BrowserRecipesBox grant={{ approved: [], staged: [SKIP_ENTRY], watchOff: [SKIP_ENTRY] }} appName="tunes" enabled />)
    expect(screen.queryByRole('switch')).toBeNull()
    rerender(<BrowserRecipesBox grant={{ approved: [SKIP_ENTRY], staged: [], watchOff: [SKIP_ENTRY] }} appName="tunes" enabled={false} />)
    expect((watchSwitch() as HTMLButtonElement).disabled || watchSwitch().getAttribute('aria-disabled') === 'true').toBe(true)
  })

  it('says an approved recipe does nothing while the preview is off', () => {
    render(<BrowserRecipesBox grant={{ approved: [JSON.stringify(PLAY)], staged: [], preview: false }} />)
    expect(screen.getByTestId('browser-recipes-preview-off').textContent).toBe(`${K}.browser_preview_off`)
  })

  it('keeps the run-by-itself yes out of the approve-all notice', () => {
    const app = { name: 'tunes', browserRecipesGrant: { approved: [SKIP_ENTRY], staged: [], watchOff: [SKIP_ENTRY] } } as unknown as AppInfo
    expect(stagedGrantsOf(app).hasStagedGrants).toBe(false)
  })

  it('renders nothing when the app declares none', () => {
    const { container } = render(<BrowserRecipesBox grant={undefined} />)
    expect(container.innerHTML).toBe('')
  })
})
