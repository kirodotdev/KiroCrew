/**
 * CrewProtocolSettings — the repo's crew protocol, on the repo's settings page.
 *
 * Five behaviours are pinned:
 *
 *   1. A write is a ONE-KEY merge patch, never the whole document. That is what
 *      stops two tabs editing different fields from erasing each other, and it is
 *      why the write path needs no revision guard.
 *   2. Committing a field UNCHANGED writes nothing, so tabbing across the form
 *      does not generate traffic or a spurious "Saved."
 *   3. The write is addressed to the repo the PAGE is for, which is not
 *      necessarily the active repo — this page can be opened for any connected
 *      repository from the rail.
 *   4. A value the store would REFUSE is refused visibly: the draft stays on
 *      screen with the constraint under the field, rather than snapping back to
 *      the saved value — which is what a successful save looks like.
 *   5. The draft is the only copy of what was typed, so it is released by the
 *      server's ANSWER, not by the submit: a failed write keeps the text on
 *      screen. Its own describe block, at the bottom, covers this.
 *
 * Assertions are on `data-testid` / `data-state`, not rendered English: i18next
 * echoes an unresolved key back, so a text assertion would pin the placeholder
 * rather than the behaviour. The one exception is the validation copy, which is
 * asserted through `i18nT` on the SAME key the component renders.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'
import type { CrewSettings } from '../apps/issue-radar/api'
import { repoScopeKey } from '../apps/issue-radar/lib/links'

// brand-ok: the repository name
const PAGE_REPO = { owner: 'kirodotdev', repo: 'KiroCrew' } // brand-ok: the repository name

const api = {
  getCrewSettings: vi.fn(),
  putCrewSettings: vi.fn(),
}
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/issue-radar/api')>()),
  issueRadarApi: api,
}))

const CrewProtocolSettings = (
  await import('../apps/issue-radar/views/settings/CrewProtocolSettings')
).default

const SETTINGS: CrewSettings = {
  schema: 1,
  claim_ttl_hours: 48,
  needs_human_label: 'crew: needs human',
  commit_trailer: 'Crew: {name} (Kiro Crew Issue Radar)',
}

/** `settings` is passed EXPLICITLY, with no default: a default parameter is used
 *  for `undefined` too, so the "still loading" case could never be reached. */
function mount(settings: CrewSettings | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CrewProtocolSettings repoRef={PAGE_REPO} settings={settings} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getCrewSettings.mockResolvedValue({ settings: SETTINGS })
  api.putCrewSettings.mockImplementation(async (_ref: unknown, patch: Partial<CrewSettings>) => ({
    settings: { ...SETTINGS, ...patch },
  }))
})

describe('CrewProtocolSettings', () => {
  it('sends a one-key merge patch for the field that changed', async () => {
    // A whole-document write would need a revision guard; a one-key merge does
    // not, and it is what makes two tabs editing different fields safe.
    mount(SETTINGS)
    const ttl = screen.getByTestId('crew-desk-claim-ttl')
    expect((ttl as HTMLInputElement).value).toBe('48')
    await userEvent.clear(ttl)
    await userEvent.type(ttl, '24')
    await userEvent.tab()
    await waitFor(() => expect(api.putCrewSettings).toHaveBeenCalledTimes(1))
    // Addressed to the repo whose settings page this is — NOT to the active repo.
    // The rail opens this page for any connected repository.
    expect(api.putCrewSettings).toHaveBeenCalledWith(PAGE_REPO, { claim_ttl_hours: 24 })
    // Exactly one key: the other two fields must not ride along.
    expect(Object.keys(api.putCrewSettings.mock.calls[0][1])).toEqual(['claim_ttl_hours'])
  })

  it('writes nothing when a field is committed unchanged', async () => {
    mount(SETTINGS)
    const trailer = screen.getByTestId('crew-desk-commit-trailer')
    expect((trailer as HTMLInputElement).value).toBe(SETTINGS.commit_trailer)
    await userEvent.click(trailer)
    await userEvent.tab()
    expect(api.putCrewSettings).not.toHaveBeenCalled()
  })

  it('reports a rejected write in place, and says so out loud', async () => {
    api.putCrewSettings.mockRejectedValue(new Error('403 forbidden'))
    mount(SETTINGS)
    const label = screen.getByTestId('crew-desk-needs-human')
    await userEvent.clear(label)
    await userEvent.type(label, 'needs: human')
    await userEvent.tab()
    const status = await screen.findByTestId('crew-desk-protocol-status')
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('failed'))
    // It updates in place, so assistive technology has to be told.
    expect(status.getAttribute('aria-live')).toBe('polite')
  })

  it('sends the needs-human label as its own one-key patch', async () => {
    // The label is what a crew applies when a call is the human's to make, so a
    // repo with two crews needs exactly one value — and editing it must not drag
    // the TTL or the trailer along.
    mount(SETTINGS)
    const label = screen.getByTestId('crew-desk-needs-human')
    expect((label as HTMLInputElement).value).toBe('crew: needs human')
    await userEvent.clear(label)
    await userEvent.type(label, 'needs: a human')
    await userEvent.tab()
    await waitFor(() => expect(api.putCrewSettings).toHaveBeenCalledTimes(1))
    expect(api.putCrewSettings).toHaveBeenCalledWith(PAGE_REPO, {
      needs_human_label: 'needs: a human',
    })
    expect(Object.keys(api.putCrewSettings.mock.calls[0][1])).toEqual(['needs_human_label'])
  })

  it('disables every field until the settings have loaded', () => {
    // Without the saved values a commit cannot tell a real edit from a no-op, so
    // the form is disabled rather than merely empty.
    mount(undefined)
    for (const id of ['crew-desk-claim-ttl', 'crew-desk-needs-human', 'crew-desk-commit-trailer']) {
      expect((screen.getByTestId(id) as HTMLInputElement).disabled).toBe(true)
    }
  })

  it('leaves the needs-human label in the app font', () => {
    // `font-mono` reads Tailwind's `--mono`, not the user's `--font-body`, so a
    // field that hardcodes it ignores the app's font setting. Only the commit
    // trailer — a git template written verbatim into a commit — opts in.
    mount(SETTINGS)
    expect(screen.getByTestId('crew-desk-needs-human').className).not.toMatch(/font-mono/)
    expect(screen.getByTestId('crew-desk-commit-trailer').className).toMatch(/font-mono/)
  })
})

/**
 * A value the store would refuse must be REFUSED VISIBLY.
 *
 * `crew_store.write_settings` takes the numeric field only `if val > 0` and a
 * trailer or a label only when it is non-blank, so the client has to stop the
 * rest. What it must not do is drop the draft while stopping it: the field then
 * snaps back to the saved value with no message, which is exactly what a
 * successful save of that value would look like.
 */
describe('CrewProtocolSettings — a refused value', () => {
  /** Set a field and commit it, the way a blur does. `fireEvent`, not
   *  `userEvent.type`: user-event enforces a number input's own validity as it
   *  goes, so an intermediate `-` is swallowed and the negative case could never
   *  be typed at all. */
  const commit = (testId: string, value: string) => {
    const input = screen.getByTestId(testId)
    fireEvent.change(input, { target: { value } })
    fireEvent.blur(input)
    return input as HTMLInputElement
  }

  /** Every value the store would drop: `> 0` for the number, non-blank for the
   *  label and the trailer. */
  const REFUSED: Array<[string, string]> = [
    ['crew-desk-claim-ttl', '0'],
    ['crew-desk-claim-ttl', '-4'],
    ['crew-desk-claim-ttl', ''],
    ['crew-desk-needs-human', '   '],
    ['crew-desk-commit-trailer', '   '],
  ]

  it.each(REFUSED)('keeps %s visible when it is set to "%s", and sends nothing', async (testId, typed) => {
    mount(SETTINGS)
    const input = commit(testId, typed)

    // Not written — the store would have dropped it anyway.
    expect(api.putCrewSettings).not.toHaveBeenCalled()
    // Still on screen, so the user can correct it instead of retyping it. This is
    // the whole finding: the old code deleted the draft here, and the field then
    // showed the saved value again, which is indistinguishable from a save.
    expect(input.value).toBe(typed)
    // And told why, next to the field, wired for assistive technology.
    const err = await screen.findByTestId(`${testId}-error`)
    expect(err).toHaveAttribute('role', 'alert')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', err.id)
    expect(err.textContent?.trim()).toBeTruthy()
  })

  it('states the constraint in the field’s own terms', async () => {
    // The bound the STORE enforces is `> 0` for the TTL and non-blank for the two
    // free-text fields. Asserted through the catalog rather than against English
    // prose, so a copy edit cannot fail a behaviour test.
    mount(SETTINGS)
    commit('crew-desk-claim-ttl', '0')
    expect(await screen.findByTestId('crew-desk-claim-ttl-error')).toHaveTextContent(
      i18nT('apps.issueRadar.views.crews.desk.claim_ttl_min'),
    )
    commit('crew-desk-needs-human', '')
    expect(await screen.findByTestId('crew-desk-needs-human-error')).toHaveTextContent(
      i18nT('apps.issueRadar.views.crews.desk.needs_human_required'),
    )
    commit('crew-desk-commit-trailer', '')
    expect(await screen.findByTestId('crew-desk-commit-trailer-error')).toHaveTextContent(
      i18nT('apps.issueRadar.views.crews.desk.trailer_required'),
    )
  })

  it('clears the message on the next keystroke, and then saves the corrected value', async () => {
    // Typing IS the user answering the message, so it must not sit there until the
    // next blur — and the correction has to actually go through.
    mount(SETTINGS)
    const ttl = commit('crew-desk-claim-ttl', '0')
    await screen.findByTestId('crew-desk-claim-ttl-error')

    fireEvent.change(ttl, { target: { value: '12' } })
    expect(screen.queryByTestId('crew-desk-claim-ttl-error')).not.toBeInTheDocument()
    expect(ttl).not.toHaveAttribute('aria-invalid')

    fireEvent.blur(ttl)
    await waitFor(() => expect(api.putCrewSettings).toHaveBeenCalledTimes(1))
    expect(api.putCrewSettings).toHaveBeenCalledWith(PAGE_REPO, { claim_ttl_hours: 12 })
  })

  it('reports only the field that was refused', async () => {
    // The three fields commit independently, so a rejected trailer must not blank
    // the message under the claim TTL, and neither may speak for the third field.
    mount(SETTINGS)
    commit('crew-desk-claim-ttl', '0')
    await screen.findByTestId('crew-desk-claim-ttl-error')

    commit('crew-desk-commit-trailer', '')
    expect(await screen.findByTestId('crew-desk-commit-trailer-error')).toBeInTheDocument()
    expect(screen.getByTestId('crew-desk-claim-ttl-error')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-desk-needs-human-error')).not.toBeInTheDocument()
  })

  it('refuses text the STORE would silently drop, instead of appearing to save it', async () => {
    // REGRESSION: past 200 characters the store reads the value as "not
    // configured" and keeps the DEFAULT, answering 200 with the setting
    // unchanged. That success released the draft, so the typed label vanished and
    // the form looked like it had saved — the operator then waits on a queue that
    // was never configured. Nothing must be sent at all.
    mount(SETTINGS)
    commit('crew-desk-needs-human', 'x'.repeat(201))
    expect(await screen.findByTestId('crew-desk-needs-human-error')).toHaveTextContent(
      i18nT('apps.issueRadar.views.crews.desk.text_too_long', { max: 200 }),
    )
    expect(api.putCrewSettings).not.toHaveBeenCalled()

    // The bound is INCLUSIVE: exactly 200 is a value the store keeps, so it must
    // go through. Without this half, rejecting everything would also pass.
    const ok = 'y'.repeat(200)
    commit('crew-desk-needs-human', ok)
    await waitFor(() => expect(api.putCrewSettings).toHaveBeenCalledTimes(1))
    expect(api.putCrewSettings).toHaveBeenCalledWith(PAGE_REPO, { needs_human_label: ok })
  })

  it('reads the scientific notation a number input hands back, rather than truncating it', async () => {
    // REGRESSION: `parseInt('1e2', 10)` is 1 — it stops at the 'e'. An
    // <input type="number"> accepts 1e2 as a valid number and returns it
    // verbatim, so a 100-hour TTL was persisted as 1: a claim expiring almost
    // immediately, silently, with the form reporting success.
    mount(SETTINGS)
    commit('crew-desk-claim-ttl', '1e2')
    await waitFor(() => expect(api.putCrewSettings).toHaveBeenCalledTimes(1))
    expect(api.putCrewSettings).toHaveBeenCalledWith(PAGE_REPO, { claim_ttl_hours: 100 })
  })

  it('refuses a fractional TTL rather than flooring it behind the user', async () => {
    // Number() admits '1.5', which parseInt would have floored to 1. Storing a
    // rounded-down value the user never typed is the same class of silent
    // substitution as the case above, so it is refused visibly instead.
    mount(SETTINGS)
    commit('crew-desk-claim-ttl', '1.5')
    expect(await screen.findByTestId('crew-desk-claim-ttl-error')).toBeInTheDocument()
    expect(api.putCrewSettings).not.toHaveBeenCalled()
  })
})

/**
 * The draft is the only copy of what the user typed, so the SERVER's answer is
 * what releases it.
 *
 * These fields are repo-wide rules — the claim TTL every crew negotiates by, the
 * trailer they sign commits with, the label they apply when a call is a human's
 * to make. Releasing the draft on submit meant a rejected write fell the field
 * back to the old saved value: the typed text was gone, and the only surviving
 * report of it was an error line next to a value the user did not choose.
 *
 * The three cases pinned here are the whole contract: a failed write KEEPS the
 * text, a landed write releases it, and a commit that changes nothing releases it
 * at once — because a draft equal to the saved value would leave the field
 * looking edited for the rest of the session.
 */
describe('CrewProtocolSettings — a draft and the answer that releases it', () => {
  const set = (testId: string, value: string) => {
    const input = screen.getByTestId(testId) as HTMLInputElement
    fireEvent.change(input, { target: { value } })
    fireEvent.blur(input)
    return input
  }

  it('keeps the typed text in the field when the write FAILS', async () => {
    api.putCrewSettings.mockRejectedValue(new Error('403 forbidden'))
    mount(SETTINGS)
    const ttl = set('crew-desk-claim-ttl', '24')

    const status = await screen.findByTestId('crew-desk-protocol-status')
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('failed'))
    // The value the user chose, not the one the server still holds. Releasing the
    // draft on submit left '48' here — indistinguishable from a save that worked,
    // except for an error line about a value no longer on screen.
    expect(ttl.value).toBe('24')
  })

  it('releases the draft and reports saved when the write LANDS', async () => {
    let landed: (v: unknown) => void = () => {}
    api.putCrewSettings.mockImplementation(() => new Promise((res) => { landed = res }))
    mount(SETTINGS)
    const ttl = set('crew-desk-claim-ttl', '24')

    const status = await screen.findByTestId('crew-desk-protocol-status')
    // In flight: the draft is still the only copy, so it is still on screen.
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('saving'))
    expect(ttl.value).toBe('24')

    landed({ settings: { ...SETTINGS, claim_ttl_hours: 24 } })

    await waitFor(() => expect(status.getAttribute('data-state')).toBe('saved'))
    // Released: the field falls back to the `settings` prop, which this harness
    // holds fixed at 48. In the app that prop is the record the write just
    // returned, so the fall-back and the typed value are the same number.
    await waitFor(() => expect(ttl.value).toBe('48'))
  })

  it('does not let a late reply overwrite a DIFFERENT field that already saved', async () => {
    // REGRESSION: `onSuccess` stored the whole response, and each response carries
    // the entire document as the server saw it when THAT write landed. Writes are
    // one per field, not one overall, so two fields can be outstanding at once —
    // and if the older reply arrives second, its pre-edit copy of the sibling
    // overwrote the newer cached value, snapping a correctly-saved field back the
    // moment anything re-read the query. Merging only the key the patch names
    // keeps every field's newest answer whatever order the replies arrive in.
    //
    // Asserted on the CACHE, not on the input: this component takes `settings` as
    // a prop, so the rendered value falls back to that fixed prop and cannot show
    // what the cache holds. The cache is what the fix changes and what the owning
    // query feeds back into the page.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const key = ['issue-radar', 'crew-settings', repoScopeKey(PAGE_REPO)]
    client.setQueryData(key, { settings: SETTINGS })

    const landers: ((v: unknown) => void)[] = []
    api.putCrewSettings.mockImplementation(
      () => new Promise((res) => { landers.push(res) }),
    )
    render(
      <QueryClientProvider client={client}>
        <CrewProtocolSettings repoRef={PAGE_REPO} settings={SETTINGS} />
      </QueryClientProvider>,
    )

    // Two DIFFERENT fields, both committed before either reply lands.
    set('crew-desk-claim-ttl', '24')
    set('crew-desk-needs-human', 'crew: over to you')
    await waitFor(() => expect(landers.length).toBe(2))

    // The LABEL's reply lands first...
    landers[1]({ settings: { ...SETTINGS, needs_human_label: 'crew: over to you' } })
    await waitFor(() =>
      expect((client.getQueryData(key) as { settings: CrewSettings }).settings.needs_human_label)
        .toBe('crew: over to you'),
    )

    // ...then the TTL's older reply, still carrying the PRE-EDIT label.
    landers[0]({ settings: { ...SETTINGS, claim_ttl_hours: 24 } })
    await waitFor(() =>
      expect((client.getQueryData(key) as { settings: CrewSettings }).settings.claim_ttl_hours)
        .toBe(24),
    )

    // Both fields hold their own newest answer. The label is the half that broke.
    const cached = (client.getQueryData(key) as { settings: CrewSettings }).settings
    expect(cached.needs_human_label).toBe('crew: over to you')
    expect(cached.claim_ttl_hours).toBe(24)
  })

  it('does not erase a NEWER draft when an older write lands', async () => {
    // REGRESSION: the in-flight guard compared the recorded draft to the value
    // being sent, so it only skipped an identical re-send. Typing again while a
    // write was outstanding overwrote the record with the NEWER text and fired a
    // second write; when the FIRST one landed, `release` matched that newer text
    // and deleted its draft — discarding the only copy of the newer edit, which is
    // precisely what holding the draft until the write lands exists to prevent.
    const landers: ((v: unknown) => void)[] = []
    api.putCrewSettings.mockImplementation(
      () => new Promise((res) => { landers.push(res) }),
    )
    mount(SETTINGS)

    const ttl = set('crew-desk-claim-ttl', '24')
    const status = await screen.findByTestId('crew-desk-protocol-status')
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('saving'))

    // Second edit to the SAME field while the first write is still outstanding.
    fireEvent.change(ttl, { target: { value: '72' } })
    fireEvent.blur(ttl)
    expect(ttl.value).toBe('72')

    // Land the FIRST write. Its release must not touch the newer text.
    landers[0]({ settings: { ...SETTINGS, claim_ttl_hours: 24 } })
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('saved'))

    // The newer value is still on screen — it is the only copy of that edit and
    // the user never saw it acknowledged.
    expect(ttl.value).toBe('72')
    // And only ONE write went out: a second concurrent PATCH on the same field is
    // what created the race in the first place.
    expect(api.putCrewSettings).toHaveBeenCalledTimes(1)
  })

  it('releases an UNCHANGED commit at once, leaving the field clean', async () => {
    // Padded, so the draft differs from the saved value while the value the store
    // would take does not. Nothing is written, and the field must not sit there
    // holding the padded text as though an edit were pending.
    mount(SETTINGS)
    const label = set('crew-desk-needs-human', `  ${SETTINGS.needs_human_label}  `)

    expect(api.putCrewSettings).not.toHaveBeenCalled()
    expect(label.value).toBe(SETTINGS.needs_human_label)
    expect(screen.getByTestId('crew-desk-protocol-status').getAttribute('data-state')).toBe('idle')
  })
})
