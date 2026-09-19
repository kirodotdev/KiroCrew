import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ChannelFolderBackfill, type BackfillReport } from '../pages/settings/ChannelFolderBackfill'
import en from '../i18n/locales/en.json'
import de from '../i18n/locales/de.json'
import es from '../i18n/locales/es.json'
import fr from '../i18n/locales/fr.json'
// Aliased: a bare `it` import shadows vitest's own `it()` and the suite
// then fails to collect with "default is not a function".
import itCatalog from '../i18n/locales/it.json'
import pt from '../i18n/locales/pt.json'
import ru from '../i18n/locales/ru.json'
import bn from '../i18n/locales/bn.json'
import hi from '../i18n/locales/hi.json'
import ja from '../i18n/locales/ja.json'
import ko from '../i18n/locales/ko.json'
import zhCN from '../i18n/locales/zh-CN.json'

/**
 * The button that files a channel's EXISTING conversations (issue #2661).
 *
 * The reason this needs its own tests rather than riding on the panels': every
 * outcome the endpoint can report is a DIFFERENT sentence, and "nothing moved"
 * arrives as a 200 with a `reason`, not as an error. A component that rendered
 * the success branch for all of them would look correct in a panel test whose
 * fixture only ever moves something.
 */

function report(over: Partial<BackfillReport> = {}): BackfillReport {
  return {
    folder_name: 'Slack',
    moved: [],
    reason: '',
    remaining: 0,
    failed: 0,
    ...over,
  }
}

function answer(body: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: () => Promise.resolve(body),
  })
}

function renderButton(props: Partial<Parameters<typeof ChannelFolderBackfill>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ChannelFolderBackfill
        namespace="slack"
        folderName="Slack"
        testId="session-folder-backfill"
        {...props}
      />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ChannelFolderBackfill', () => {
  it('posts the channel namespace when clicked', async () => {
    const fetchMock = answer(report({ moved: [] }))
    vi.stubGlobal('fetch', fetchMock)
    renderButton({ namespace: 'telegram' })

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/channel-folders/backfill')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ namespace: 'telegram' })
  })

  it('names every session it moved', async () => {
    // The list is the only record of what happened: there is no bulk undo, so a
    // count alone would leave the user unable to put one back.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [
            { key: 'slack:1', title: 'Standup', label: 'Slack' },
            { key: 'slack:2', title: 'Release plan', label: 'Slack' },
          ],
        }),
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText('Standup')).toBeTruthy()
    expect(screen.getByText('Release plan')).toBeTruthy()
    expect(screen.getByTestId('backfill-result')).toBeTruthy()
    // The count carries a noun and agrees with it: a bare "Moved 2." leaves a
    // cold reader asking "two of what". The destination is named as a FOLDER
    // because a bare "into Slack" reads as the Slack app, not a sidebar folder.
    expect(screen.getByText(/Moved 2 sessions into the Slack folder\./)).toBeTruthy()
  })

  it('agrees with a single moved session', async () => {
    vi.stubGlobal(
      'fetch',
      answer(report({ moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }] })),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Moved 1 session into the Slack folder\./)).toBeTruthy()
  })

  it('collapses a long list but keeps every name reachable', async () => {
    const moved = Array.from({ length: 11 }, (_, i) => ({
      key: `slack:${i}`,
      title: `Chat ${i}`,
      label: 'Slack',
    }))
    vi.stubGlobal('fetch', answer(report({ moved })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText('Chat 0')).toBeTruthy()
    // Eight named up front, so the panel's own controls stay on screen.
    expect(screen.queryByText('Chat 8')).toBeNull()

    // The rest is one click away, not withheld: this list is the only
    // record of a move that has no bulk undo, so a run that moved 200
    // must not be the run whose receipt is mostly missing.
    const expand = screen.getByTestId('backfill-expand')
    expect(expand.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(expand)

    expect(screen.getByText('Chat 8')).toBeTruthy()
    expect(screen.getByText('Chat 10')).toBeTruthy()
    expect(expand.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(expand)
    expect(screen.queryByText('Chat 8')).toBeNull()
  })

  it('offers no expander when the whole list already fits', async () => {
    const moved = Array.from({ length: 8 }, (_, i) => ({
      key: `slack:${i}`,
      title: `Chat ${i}`,
      label: 'Slack',
    }))
    vi.stubGlobal('fetch', answer(report({ moved })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText('Chat 7')).toBeTruthy()
    expect(screen.queryByTestId('backfill-expand')).toBeNull()
  })

  it('gives a session with no title something to read', async () => {
    vi.stubGlobal(
      'fetch',
      answer(report({ moved: [{ key: 'slack:1', title: '', label: 'Slack' }] })),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Untitled Slack session/i)).toBeTruthy()
  })

  it('says nothing moved rather than showing an empty success', async () => {
    vi.stubGlobal('fetch', answer(report({ moved: [] })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Nothing to move/i)).toBeTruthy()
    expect(screen.queryByTestId('backfill-result')).toBeNull()
  })

  it('tells the user to save the folder name when the setting is off', async () => {
    vi.stubGlobal('fetch', answer(report({ reason: 'not_configured' })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Save the folder name first/i)).toBeTruthy()
  })

  it('names the missing folder when config points at one that is gone', async () => {
    vi.stubGlobal(
      'fetch',
      answer(report({ reason: 'folder_missing', folder_name: 'Team chat' })),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/No folder named Team chat exists yet/i)).toBeTruthy()
  })

  it('renders a store failure on the error surface, not as a plain note', async () => {
    // `unavailable` is a 200 carrying a FAILURE (store unreachable, listing
    // raised, or every write failed), so it belongs on the error surface. The
    // sibling `not_configured` / `folder_missing` outcomes are guidance and stay
    // plain notes, which is why this is asserted by testid rather than by text.
    vi.stubGlobal('fetch', answer(report({ reason: 'unavailable' })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByTestId('backfill-store-failure')).toBeTruthy()
  })

  it('keeps the two actionable outcomes off the error surface', async () => {
    vi.stubGlobal('fetch', answer(report({ reason: 'not_configured' })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Save the folder name first/i)).toBeTruthy()
    expect(screen.queryByTestId('backfill-store-failure')).toBeNull()
  })

  it('invites a second click when the run was capped', async () => {
    // The endpoint bounds one click, so a partial pass has to be visible as
    // partial -- otherwise the remaining conversations look like refusals.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }],
          remaining: 12,
          failed: 0,
        }),
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/12 still unfiled/i)).toBeTruthy()
    // It says WHY a second click helps, and it must not claim anything failed.
    expect(screen.getByText(/limited batch/i)).toBeTruthy()
    expect(screen.queryByText(/could not be moved/i)).toBeNull()
    // A capped run failed at nothing, so it must NOT reach the error surface.
    expect(screen.queryByTestId('backfill-write-failures')).toBeNull()
  })

  it('says plainly when every outstanding conversation failed to move', async () => {
    // With `failed === remaining` the earlier wording made the reader subtract
    // one count from the other to work out whether retrying could help. It says
    // it directly now. And a write failure belongs on the ERROR surface, not in
    // a muted status note -- rendering a failure as a status is the silent
    // -failure class, and it is the same mistake this component already made
    // once on its `unavailable` branch.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }],
          remaining: 4,
          failed: 4,
        }),
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Could not move 4 sessions/i)).toBeTruthy()
    expect(screen.getByTestId('backfill-write-failures')).toBeTruthy()
    // Neither the routine-batching wording nor the subtract-it-yourself one.
    expect(screen.queryByText(/limited batch/i)).toBeNull()
    // The MIXED copy must not also render. Matched on its distinguishing clause,
    // because the phrase this once matched no longer exists in any catalog, so the
    // assertion would otherwise pass whatever the component did.
    expect(screen.queryByText(/retry those and continue with the rest/i)).toBeNull()
  })

  it('keeps both counts when only some of the remainder failed', async () => {
    // Here the two causes genuinely coexist: 12 outstanding, 4 of them failures
    // and the other 8 merely waiting for another click. That case still needs
    // both numbers, and it is still a failure, so it is still an error.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }],
          remaining: 12,
          failed: 4,
        }),
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    // Leads with the failure and names BOTH effects of another click: the reader
    // could not tell from the previous wording whether one more click continues,
    // retries, or both.
    expect(
      await screen.findByText(/Could not move 4 of the 12 still unfiled/i),
    ).toBeTruthy()
    expect(screen.getByText(/retry those and continue with the rest/i)).toBeTruthy()
    expect(screen.getByTestId('backfill-write-failures')).toBeTruthy()
    expect(screen.queryByText(/limited batch/i)).toBeNull()
  })

  it('keeps the moved receipt when a terminal reason arrives with it', async () => {
    // The backend can answer with BOTH: a folder deleted mid-pass sets
    // `folder_missing` after conversations were already filed, which is the path
    // `test_a_folder_deleted_mid_pass_strands_nothing` exercises. The named list
    // is the only record of what moved -- there is no bulk undo -- so rendering
    // the note INSTEAD of the list discarded exactly what the user needs to put
    // those conversations back by hand.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [
            { key: 'slack:1', title: 'Standup', label: 'Slack' },
            { key: 'slack:2', title: 'Release plan', label: 'Slack' },
          ],
          reason: 'folder_gone',
        }),
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    // The note must say the folder went away DURING the run. The
    // not-created-yet wording would contradict the receipt sitting under it.
    expect(
      await screen.findByText(/The Slack folder was removed while this ran/i),
    ).toBeTruthy()
    expect(screen.queryByText(/exists yet/i)).toBeNull()
    // Both names, not just the note.
    expect(screen.getByText('Standup')).toBeTruthy()
    expect(screen.getByText('Release plan')).toBeTruthy()
    expect(screen.getByTestId('backfill-result')).toBeTruthy()
  })

  it('names the count when every write failed and nothing moved', async () => {
    // The backend reaches this by counting write failures, finding nothing moved
    // and no reason, and setting `all_failed`. The component used to return on
    // `moved.length === 0` before the failure notice, so this case lost the COUNT
    // and showed the read-failure sentence instead -- naming the wrong cause,
    // since every read here succeeded.
    vi.stubGlobal('fetch', answer(report({ reason: 'all_failed', remaining: 3, failed: 3 })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/Could not move 3 sessions/i)).toBeTruthy()
    expect(screen.getByTestId('backfill-write-failures')).toBeTruthy()
    // NOT the read-failure sentence, and not its surface either.
    expect(screen.queryByText(/could not read the session history/i)).toBeNull()
    expect(screen.queryByTestId('backfill-store-failure')).toBeNull()
    // And not "nothing to move", which is the opposite of what happened.
    expect(screen.queryByText(/Nothing to move/i)).toBeNull()
  })

  it('keeps the failure count when the folder vanished before anything moved', async () => {
    // A write failed, then the folder went away, so the pass broke with an empty
    // `moved` AND a non-zero `failed`. Both facts matter and neither replaces the
    // other: where the folder went, and that a write failed on the way there.
    //
    // This test previously asserted the not-created-yet sentence, which is how the
    // defect survived: `failed > 0` proves a write was ATTEMPTED, which proves the
    // lookup found the folder, so "No folder named Slack exists yet. Save these
    // settings to create it." tells the user to create a folder they did create --
    // and prescribes saving where the count below prescribes retrying.
    vi.stubGlobal(
      'fetch',
      answer(report({ reason: 'folder_gone', remaining: 2, failed: 2 })),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(
      await screen.findByText(/The Slack folder was removed while this ran, so nothing was filed/i),
    ).toBeTruthy()
    expect(screen.queryByText(/exists yet/i)).toBeNull()
    // Not the sentence for the case that DID file something: it ends "These
    // sessions had already been filed into it", which names nothing here.
    expect(screen.queryByText(/had already been filed/i)).toBeNull()
    expect(screen.getByText(/Could not move 2 sessions/i)).toBeTruthy()
    expect(screen.getByTestId('backfill-write-failures')).toBeTruthy()
  })

  it('still says the folder was never created when nothing was attempted', async () => {
    // The control for the test above. Same reason value, but with no write
    // attempted there is no evidence the folder ever existed, so the
    // not-created-yet sentence is the true one and its remedy is the only remedy.
    vi.stubGlobal('fetch', answer(report({ reason: 'folder_missing' })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/No folder named Slack exists yet/i)).toBeTruthy()
    expect(screen.queryByText(/was removed while this ran/i)).toBeNull()
    expect(screen.queryByTestId('backfill-write-failures')).toBeNull()
  })

  it('never nests an assertive alert inside a polite status region', async () => {
    // `ErrorNotice` carries `role="alert"`. An alert nested inside a `role="status"`
    // region is announced as part of that polite region instead of interrupting, so
    // the failure the user needs to hear is delivered as routine progress -- which
    // is why `errors-use-error-notice` is a blocking rule. The receipt wrapper used
    // to carry the status role while also holding the failure notice.
    //
    // Asserted structurally rather than against one testId: any future child that
    // renders an error inside the receipt reddens this, not just today's.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }],
          remaining: 3,
          failed: 2,
        }),
      ),
    )
    const { container } = renderButton()

    fireEvent.click(screen.getByRole('button'))
    await screen.findByTestId('backfill-result')

    const alerts = Array.from(container.querySelectorAll('[role="alert"]'))
    expect(alerts.length, 'the failure must render as an alert at all').toBeGreaterThan(0)
    for (const alert of alerts) {
      expect(
        alert.parentElement?.closest('[role="status"]') ?? null,
        'an alert is nested inside a status region',
      ).toBeNull()
    }
    // The receipt itself still announces politely -- the role moved, it did not go.
    expect(container.querySelector('[role="status"]')).toBeTruthy()
  })

  it('wraps a lone notice in a block so it cannot share the button row', async () => {
    // The structural half of the placement fix, citable here. A bare fragment let a
    // single notice lay out on the button's own row while the same notice beside a
    // receipt laid out below it, and one card only looked right because its plain
    // note happens to be a block.
    //
    // The GEOMETRIC half is deliberately NOT asserted here: jsdom has no layout
    // engine, so `getBoundingClientRect` answers zeros and any edge comparison would
    // pass whatever the markup did. That half is measured in the throwaway capture
    // harness that produces the attached evidence sheet, against a real engine.
    vi.stubGlobal('fetch', answer(report({ reason: 'all_failed', remaining: 2, failed: 2 })))
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    const notices = await screen.findByTestId('backfill-notices')
    const failure = screen.getByTestId('backfill-write-failures')
    expect(notices.contains(failure), 'the failure notice must sit inside the block').toBe(true)
    // A block, not an inline box: an inline wrapper would flow onto the button's row
    // exactly as the fragment did.
    expect(notices.tagName).toBe('DIV')
  })

  it('does not promise that filed sessions reappear when the folder is recreated', async () => {
    // They cannot. `ensure_channel_folder` mints a fresh `uuid4().hex[:12]` for a
    // folder that is gone, and nothing re-associates conversations already stamped
    // with the dead id -- which this repo's own
    // `test_a_folder_deleted_mid_pass_strands_nothing` states as "NO recovery".
    // An earlier revision of this copy told the user they would appear inside.
    vi.stubGlobal(
      'fetch',
      answer(
        report({
          moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }],
          reason: 'folder_gone',
        }),
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/must be moved by hand/i)).toBeTruthy()
    expect(screen.queryByText(/will appear inside/i)).toBeNull()
    // The no-receipt sibling DOES keep the save remedy, because nothing was stamped
    // there, so recreating the folder and clicking again really does work.
    expect(
      panelCopy().backfill_folder_gone_nothing_filed.toLowerCase().includes('save these settings'),
    ).toBe(true)
  })

  it('invalidates the folder list and nothing slot-shaped', async () => {
    // `['slots']` and `['chat-slots']` are both dead keys: nothing registers a
    // query on either, so invalidating one is a silent no-op -- no request and no
    // error (#10204, with a tree-wide ratchet in
    // `chatSlotsDeadKeyInvalidation.test.tsx`). A slot row renders from the Redux
    // `dashboard` slice fed by the websocket `sseSlots` frame, and this endpoint
    // ends in `push_slots_update()` whenever it touched a live slot, so the
    // refresh already arrives without the client asking. `['chat-folders']` IS
    // registered (`SessionActionsMenu`, `JobForm`), so that one does real work.
    //
    // Asserted on the keys the component passes, because a dead key is invisible
    // in rendered output -- which is the whole defect class.
    const invalidated: string[] = []
    const spy = vi
      .spyOn(QueryClient.prototype, 'invalidateQueries')
      .mockImplementation((filters: unknown) => {
        invalidated.push(JSON.stringify((filters as { queryKey?: unknown })?.queryKey))
        return Promise.resolve()
      })
    try {
      vi.stubGlobal(
        'fetch',
        answer(report({ moved: [{ key: 'slack:1', title: 'A', label: 'Slack' }] })),
      )
      renderButton()

      fireEvent.click(screen.getByRole('button'))
      await screen.findByTestId('backfill-result')

      expect(invalidated, 'the folder list must be invalidated').toContain('["chat-folders"]')
      const slotish = invalidated.filter(k => /slots/i.test(k))
      expect(slotish, 'no dead slot-shaped key may be invalidated').toEqual([])
    } finally {
      spy.mockRestore()
    }
  })

  it('surfaces a refusal from the endpoint', async () => {
    // The endpoint's refusal is a whole sentence naming the remedy, so the panel
    // passes it through unchanged rather than substituting its own.
    vi.stubGlobal(
      'fetch',
      answer(
        {
          error:
            'Filing runs only on the computer that hosts this dashboard. ' +
            'Open the dashboard there and click again.',
        },
        false,
        403,
      ),
    )
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    expect(await screen.findByText(/hosts this dashboard/i)).toBeTruthy()
    expect(screen.getByText(/Open the dashboard there and click again/i)).toBeTruthy()
  })

  it('drops a previous result before the next run rather than after it', async () => {
    // A stale "moved 2" sitting under a running button reads as this run's result.
    let release: (v: unknown) => void = () => {}
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve(report({ moved: [{ key: 'slack:1', title: 'Standup', label: 'Slack' }] })),
      })
      .mockImplementationOnce(() => new Promise(r => (release = r)))
    vi.stubGlobal('fetch', fetchMock)
    renderButton()

    fireEvent.click(screen.getByRole('button'))
    expect(await screen.findByText('Standup')).toBeTruthy()

    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(screen.queryByText('Standup')).toBeNull())
    release({ ok: true, status: 200, json: () => Promise.resolve(report({ moved: [] })) })
  })

  it('cannot be clicked while a run is in flight', async () => {
    let release: (v: unknown) => void = () => {}
    const fetchMock = vi.fn().mockImplementation(() => new Promise(r => (release = r)))
    vi.stubGlobal('fetch', fetchMock)
    renderButton()

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(screen.getByRole('button')).toBeDisabled())
    fireEvent.click(screen.getByRole('button'))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    release({ ok: true, status: 200, json: () => Promise.resolve(report()) })
  })

  it('is inert on a read-only remote session', () => {
    const fetchMock = answer(report())
    vi.stubGlobal('fetch', fetchMock)
    renderButton({ disabled: true })

    expect(screen.getByRole('button')).toBeDisabled()
    fireEvent.click(screen.getByRole('button'))
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

/**
 * Copy consistency ACROSS a key's plural variants, which no component test can
 * reach: a locale renders only the category its count selects, so a wrong
 * variant is invisible until a user hits that count.
 *
 * This exists because it happened. Two rename passes changed `_one` and
 * `_other` -- the only categories English has -- and left `_many` in es/fr/it/pt
 * and `_few`/`_many` in ru carrying the superseded noun AND missing the folder
 * noun. Russian's `_few` fires at 2-4 and `_many` at 5-20, so those were
 * reachable. A check that reads only English's categories cannot see this class,
 * which is exactly the mistake the first verification pass made.
 *
 * The invariant is language-agnostic: plural category changes how the COUNT
 * agrees, never the phrase that introduces `{{folder}}`. So every variant of one
 * key must carry the same placeholders and the same lead-in to that placeholder.
 */
function panelCopy(): Record<string, string> {
  const c = en as { pages: { settings: { botChannelPanel: Record<string, string> } } }
  return c.pages.settings.botChannelPanel
}

describe('backfill copy is consistent across plural variants', () => {
  const CATALOGS: Record<string, unknown> = {
    en, de, es, fr, pt, ru, bn, hi, ja, ko, it: itCatalog, 'zh-CN': zhCN,
  }

  function panelOf(catalog: unknown): Record<string, string> {
    const c = catalog as { pages: { settings: { botChannelPanel: Record<string, string> } } }
    return c.pages.settings.botChannelPanel
  }

  function variantsOf(panel: Record<string, string>, base: string) {
    return Object.entries(panel).filter(([k]) => k.startsWith(`${base}_`))
  }

  const placeholders = (s: string) => (s.match(/\{\{\w+\}\}/g) ?? []).sort().join(',')

  /** The two words immediately before `{{folder}}`, which name the destination. */
  function folderLeadIn(s: string) {
    const i = s.indexOf('{{folder}}')
    if (i < 0) return null
    return s.slice(0, i).trim().split(/\s+/).slice(-2).join(' ')
  }

  for (const base of ['backfill_moved', 'backfill_failed_all']) {
    it(`${base}: every locale's variants agree on placeholders and the folder phrase`, () => {
      const checked: string[] = []
      for (const [locale, catalog] of Object.entries(CATALOGS)) {
        const variants = variantsOf(panelOf(catalog), base)
        expect(variants.length, `${locale} has no ${base} variants`).toBeGreaterThan(0)
        const first = variants[0][1]
        for (const [key, value] of variants) {
          expect(value.trim(), `${locale}:${key} is blank`).not.toBe('')
          expect(placeholders(value), `${locale}:${key} placeholder set`).toBe(
            placeholders(first),
          )
          expect(folderLeadIn(value), `${locale}:${key} folder phrase`).toBe(
            folderLeadIn(first),
          )
          checked.push(`${locale}:${key}`)
        }
      }
      // A reachability control: a walk that checked nothing would pass every
      // assertion above.
      expect(checked.length).toBeGreaterThanOrEqual(12)
    })
  }

  it('every locale can name the folder in all three folder-absence sentences', () => {
    // Two reason values, three sentences: never created, deleted with a receipt,
    // deleted with nothing filed. A catalog missing any one silently falls back to
    // English or renders a bare key. Language-agnostic on purpose: the placeholder
    // is the only thing every locale must share.
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      for (const key of [
        'backfill_folder_gone',
        'backfill_folder_gone_nothing_filed',
        'backfill_folder_missing',
      ]) {
        const value = panelOf(catalog)[key]
        expect(value, `${locale} is missing ${key}`).toBeTruthy()
        expect(value, `${locale}:${key} cannot name the folder`).toContain('{{folder}}')
      }
    }
  })

  it('the count-bearing failure strings keep the placeholders the panel supplies', () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const panel = panelOf(catalog)
      expect(panel.backfill_remaining, `${locale} backfill_remaining`).toContain('{{count}}')
      const mixed = panel.backfill_remaining_failed
      expect(mixed, `${locale} backfill_remaining_failed`).toContain('{{count}}')
      expect(mixed, `${locale} backfill_remaining_failed`).toContain('{{failed}}')
      for (const [key, value] of variantsOf(panel, 'backfill_failed_all')) {
        expect(value, `${locale}:${key}`).toContain('{{count}}')
        // The all-failed line must NOT take `{{failed}}`: the panel passes only
        // `count` there, so a `{{failed}}` would render as raw text.
        expect(value, `${locale}:${key}`).not.toContain('{{failed}}')
      }
    }
  })
})
