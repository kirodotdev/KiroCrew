import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'

import { renderWithProviders, createTestStore } from '../test/helpers'
import { setActiveSlot } from '../store/chatSlice'
import { tipDocHref } from '../utils/docsLink'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { findReport, __resetErrorJournalForTests } from '../utils/errorReport'
import { resetStartupVideoLaunchGuardForTests, startupVideoHandledThisLaunch } from './startupVideoGate'
import type { FeatureVideo } from '../api/client'
import StartupVideoModal from './StartupVideoModal'

vi.mock('../api/client', () => {
  class MockApiError extends Error {}
  return {
    ApiError: MockApiError,
    api: {
      featureVideoNext: vi.fn(),
      featureVideoFeedback: vi.fn(),
      featureVideoProbe: vi.fn(),
    },
  }
})

/**
 * The share card is stubbed rather than rendered: it drags in `html-to-image` and
 * a canvas export path that has nothing to do with this modal's behaviour, and
 * the PROP CONTRACT between the two is already enforced by `tsc` (the real
 * `ShareMessageModalProps` is required at the call site). What these tests own is
 * whether the section is reachable at all.
 */
vi.mock('../pages/chat/share/ShareMessageModal', () => ({
  default: ({ messageText, prevUserText, copy, shareEnabled }: {
    messageText: string
    prevUserText?: string
    copy?: { description?: string; includeQuestion?: string; caption?: string }
    shareEnabled: boolean
  }) => (
    <div data-testid="stub-share-modal">
      <span data-testid="stub-share-title">{prevUserText}</span>
      <span data-testid="stub-share-body">{messageText}</span>
      {/* Echoed so a test can assert the host passed surface-appropriate wording
          rather than letting the chat defaults through. */}
      <span data-testid="stub-share-copy-description">{copy?.description}</span>
      <span data-testid="stub-share-copy-include">{copy?.includeQuestion}</span>
      {/* The text the X / LinkedIn composer and the clipboard actually receive.
          The card excerpt (`messageText`) is a separate channel. */}
      <span data-testid="stub-share-copy-caption">{copy?.caption}</span>
      {/* The real card withdraws its own actions when this is false. A test
          asserts the host keeps FEEDING it the live answer instead of pulling
          the card out from under an export already in flight. */}
      <span data-testid="stub-share-enabled">{String(shareEnabled)}</span>
      {/* Stands in for the real card's intent buttons, so a test can assert on
          the same testids the shipped card uses. */}
      <button data-testid="share-x">x</button>
      <button data-testid="share-linkedin">linkedin</button>
    </div>
  ),
}))

const mockedApi = vi.mocked(api)

const clip: FeatureVideo = {
  id: 'vid-1',
  feature: 'startup-videos',
  title: 'Feature videos',
  description: 'A short clip introduces each new feature.',
  src: '/app-assets/feature-videos/placeholder.mp4',
  poster: '/app-assets/feature-videos/placeholder.jpg',
  duration_s: 10,
  // The real shape: `feature_videos.CATALOG` stores a bare docs filename, not a
  // URL, and ships no resolved `doc_link` beside it.
  doc: 'feature-tips.md',
  // Cached on this machine, which is the ordinary case: a same-origin path under
  // the release folder, and no network cost to open it.
  source: 'local',
}

/** The same clip still on the CDN: an absolute URL, and streamed to play. */
const remoteClip: FeatureVideo = {
  ...clip,
  src: 'https://cdn.example.invalid/feature-videos/2026.09.1/placeholder.mp4',
  poster: 'https://cdn.example.invalid/feature-videos/2026.09.1/placeholder.jpg',
  source: 'remote',
}

const dialog = () => screen.queryByRole('dialog')
const video = () => screen.queryByTestId('startup-video') as HTMLVideoElement | null

/**
 * Mount and settle. The open path spans two async hops (the query resolving,
 * then the render it enables), so a single flush can sample between them and let
 * a "stays closed" assertion pass without ever having had the chance to open.
 */
async function mount(props: Partial<React.ComponentProps<typeof StartupVideoModal>> = {}) {
  const onClose = props.onClose ?? vi.fn()
  const rendered = renderWithProviders(
    <StartupVideoModal {...props} onClose={onClose} />,
  )
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 5)) })
  }
  return { ...rendered, onClose }
}

/**
 * A stray `fetch` must not reach the network from a test. Nothing in the component
 * calls it any more -- reachability is asked of the server through `api` -- so this
 * stub exists to FAIL LOUDLY if something starts doing so again, rather than
 * silently answering it.
 */
const strayFetch = vi.fn<typeof fetch>()

beforeEach(() => {
  mockedApi.featureVideoNext.mockReset()
  mockedApi.featureVideoFeedback.mockReset()
  mockedApi.featureVideoProbe.mockReset()
  mockedApi.featureVideoFeedback.mockResolvedValue({ ok: true } as never)
  // Downloads permitted by default: it is what makes the remote cases below
  // openable at all, and it is irrelevant to a local clip.
  mockedApi.featureVideoNext.mockResolvedValue({
    video: clip, enabled: true, download_enabled: true,
  } as never)
  mockedApi.featureVideoProbe.mockResolvedValue({ ok: true } as never)
  strayFetch.mockReset()
  strayFetch.mockRejectedValue(new Error('no test may reach the network'))
  vi.stubGlobal('fetch', strayFetch)
  resetStartupVideoLaunchGuardForTests()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('StartupVideoModal — when it renders nothing', () => {
  it('renders nothing when the backend has no video to offer', async () => {
    // The steady state for most launches, and NOT an error: every clip has been
    // seen or dismissed already.
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    expect(dialog()).not.toBeInTheDocument()
  })

  it('renders nothing when the feature is disabled, even with a video attached', async () => {
    // `enabled` is the operator kill switch and outranks the payload; a disabled
    // install that still names a clip must stay silent.
    mockedApi.featureVideoNext.mockResolvedValue({ video: clip, enabled: false } as never)
    await mount()
    expect(dialog()).not.toBeInTheDocument()
  })

  it('renders nothing when the request fails (404 from an older gateway)', async () => {
    mockedApi.featureVideoNext.mockRejectedValue(new Error('HTTP 404'))
    await mount()
    expect(dialog()).not.toBeInTheDocument()
  })

  it('posts no verdict on any of those paths', async () => {
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    // A launch that showed nothing must not retire anything — otherwise the clip
    // is burned without the user ever seeing it.
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })
})

describe('StartupVideoModal — the reachability probe (remote clips only)', () => {
  /** Every case here is about a streamed clip; a local one never probes. */
  const mountRemote = (props = {}) => {
    mockedApi.featureVideoNext.mockResolvedValue({
      video: remoteClip, enabled: true, download_enabled: true,
    } as never)
    return mount(props)
  }

  it('does not probe a CACHED clip at all', async () => {
    // The backend's `offerable()` only offers a clip whose files `_asset_exists`
    // found on disk, in the same request that offered this one. A probe here would
    // re-run that check and learn nothing, so it is not asked -- and the dialog
    // opens on the offer alone, exactly as it did before the route existed.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(mockedApi.featureVideoProbe).not.toHaveBeenCalled()
    expect(video()).toHaveAttribute('src', clip.src)
  })

  it('asks the SERVER once, by clip id, and opens a streamed clip on ok', async () => {
    await mountRemote()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(mockedApi.featureVideoProbe).toHaveBeenCalledTimes(1)
    expect(mockedApi.featureVideoProbe).toHaveBeenCalledWith(remoteClip.id, undefined)
    expect(video()).toBeInTheDocument()
  })

  it('never probes the asset from the page', async () => {
    // The regression guard for the whole change. A HEAD from the page cannot answer
    // this question for a CDN clip -- it is cross-origin, so the browser reports a
    // network failure for a perfectly healthy file.
    await mountRemote()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(strayFetch).not.toHaveBeenCalled()
  })

  it('does not probe when the gate would not open the dialog anyway', async () => {
    // The probe is the price of a launch that WOULD show a streamed clip. The
    // common launch -- nothing to offer -- must stay at one JSON round trip.
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    expect(mockedApi.featureVideoProbe).not.toHaveBeenCalled()
  })

  it('on `ok: false`: no dialog, no verdict, error journaled, launch spent', async () => {
    __resetErrorJournalForTests()
    mockedApi.featureVideoProbe.mockResolvedValue({ ok: false } as never)
    const { onClose } = await mountRemote()

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(dialog()).not.toBeInTheDocument()
    // No `<video>` was ever mounted: a player whose bytes are known to be missing
    // must not get as far as the DOM.
    expect(video()).not.toBeInTheDocument()
    // Neither verdict. `dismissed` is permanent and the user never saw the clip.
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
    // Same journal entry as the mid-playback `onError` path, and `endpoint` names
    // the CLIP rather than the probe route -- the route worked; the asset did not.
    const report = findReport(i18nT('components.startupVideoModal.media_failed'))
    expect(report).toBeDefined()
    expect(report?.endpoint).toBe(remoteClip.src)
    expect(report?.source).toBe('api')
    expect(report?.code).toBe('probe_not_ok')
    // The launch is claimed, so a host that re-mounted would not probe again.
    expect(startupVideoHandledThisLaunch()).toBe(true)
  })

  it('on a probe that cannot be asked: same outcome, reason in `detail`', async () => {
    // The network, or a gateway with no such route. Either way this clip streams
    // from somewhere the page cannot verify, and an unanswerable question is not
    // permission to open a player.
    __resetErrorJournalForTests()
    mockedApi.featureVideoProbe.mockRejectedValue(new Error('HTTP 503'))
    const { onClose } = await mountRemote()

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(dialog()).not.toBeInTheDocument()
    expect(video()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
    const report = findReport(i18nT('components.startupVideoModal.media_failed'))
    expect(report).toBeDefined()
    expect(report?.endpoint).toBe(remoteClip.src)
    expect(report?.code).toBe('probe_failed')
    expect(report?.detail).toContain('HTTP 503')
    expect(startupVideoHandledThisLaunch()).toBe(true)
  })

  it('probes exactly once per mount, even across re-renders', async () => {
    const { rerender } = await mountRemote()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    rerender(<StartupVideoModal shareEnabled onClose={vi.fn()} />)
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    expect(mockedApi.featureVideoProbe).toHaveBeenCalledTimes(1)
  })

  it('carries the active slot key on the probe', async () => {
    // The probe reads per-session state on the server the same way the metadata
    // read does, so it must name the same session.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: remoteClip, enabled: true, download_enabled: true,
    } as never)
    const store = createTestStore()
    store.dispatch(setActiveSlot('slot-9'))
    renderWithProviders(<StartupVideoModal onClose={vi.fn()} />, { store })
    for (let i = 0; i < 6; i++) {
      await act(async () => { await new Promise(r => setTimeout(r, 5)) })
    }
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(mockedApi.featureVideoProbe).toHaveBeenCalledWith(remoteClip.id, 'dashboard:slot-9')
  })
})

describe('StartupVideoModal — a clip that streams from the CDN', () => {
  const streamingHint = () => screen.queryByTestId('startup-video-streaming')

  beforeEach(() => {
    mockedApi.featureVideoNext.mockResolvedValue({
      video: remoteClip, enabled: true, download_enabled: true,
    } as never)
  })

  it('plays the URL the backend gave it, unchanged', async () => {
    // The client composes no URL, ever. It is the single rule that keeps a config
    // value from pointing the player at a host nobody chose.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    expect(video()).toHaveAttribute('src', remoteClip.src)
    expect(video()).toHaveAttribute('poster', remoteClip.poster)
  })

  it('preloads NOTHING, so the chip discloses a cost not yet spent', async () => {
    // MUTATION-VERIFIED: set `preload="metadata"` for a remote clip and this
    // fails. Reading the header would spend CDN bytes on a launch nobody pressed
    // play on, which is both the invariant this component documents and the thing
    // the "Plays online" chip promises has not happened yet. The cost of holding
    // the line is a streamed clip showing no duration until it plays.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    expect(video()).toHaveAttribute('preload', 'none')
    expect(video()).not.toHaveAttribute('autoplay')
  })

  it('says on the card that the clip streams', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(streamingHint()).toBeInTheDocument()
    expect(streamingHint()).toHaveTextContent(i18nT('components.startupVideoModal.streaming'))
  })

  it('says nothing of the sort for a cached clip', async () => {
    // There is no cost to disclose, so there is no hint. A badge on every clip
    // would carry no information at all.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: clip, enabled: true, download_enabled: true,
    } as never)
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(streamingHint()).not.toBeInTheDocument()
    expect(video()).toHaveAttribute('preload', 'none')
  })

  it('does not probe a cached clip even while remote clips do', async () => {
    // The asymmetry stated as its own guard: which source is probed is decided by
    // `source`, not by whether a probe happens to be mocked.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: clip, enabled: true, download_enabled: true,
    } as never)
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(mockedApi.featureVideoProbe).not.toHaveBeenCalled()
  })

  it('does not open, and does not probe, when downloads are forbidden', async () => {
    // Fail closed. With downloads off there is no route to the bytes, so opening
    // would hand the user a player that can never fill -- and the probe would be
    // spent asking about a clip that cannot be shown either way.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: remoteClip, enabled: true, download_enabled: false,
    } as never)
    const { onClose } = await mount()
    expect(dialog()).not.toBeInTheDocument()
    expect(video()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoProbe).not.toHaveBeenCalled()
    // Not a decision about the clip: no verdict, so it is offered again once the
    // policy allows it.
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('treats an ABSENT download answer as no, not as yes', async () => {
    // A gateway that predates the field, or a read that failed. Fail-closed means
    // `=== true`, so silence must not read as permission.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: remoteClip, enabled: true,
    } as never)
    await mount()
    expect(dialog()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoProbe).not.toHaveBeenCalled()
  })

  it('still shows a CACHED clip when downloads are forbidden', async () => {
    // The gate is about fetching bytes over the network. A file already on disk
    // needs none, so the policy has nothing to say about it -- and blocking it
    // would retire the whole feature on a locked-down install.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: clip, enabled: true, download_enabled: false,
    } as never)
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(video()).toHaveAttribute('src', clip.src)
  })
})

describe('StartupVideoModal — the clip', () => {
  it('opens with the clip title, blurb, poster and source', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText(clip.title)).toBeInTheDocument()
    expect(screen.getByText(clip.description)).toBeInTheDocument()
    const el = video()
    expect(el).toBeInTheDocument()
    expect(el).toHaveAttribute('src', clip.src)
    expect(el).toHaveAttribute('poster', clip.poster)
  })

  it('names and describes itself for assistive tech', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const d = dialog() as HTMLElement
    expect(d).toHaveAttribute('aria-modal', 'true')
    // Named by the clip's own heading rather than a generic label, so the
    // announcement says which feature this is about.
    const labelId = d.getAttribute('aria-labelledby')
    const descId = d.getAttribute('aria-describedby')
    expect(labelId).toBeTruthy()
    expect(descId).toBeTruthy()
    expect(document.getElementById(labelId as string)).toHaveTextContent(clip.title)
    expect(document.getElementById(descId as string)).toHaveTextContent(clip.description)
  })

  it('moves keyboard focus into the dialog once it appears', async () => {
    // The dialog's first commit renders NOTHING: the clip arrives from a query, so
    // there is no dialog element yet. Focus entry must survive that gap. If it is
    // wired to the component's mount instead of the dialog's appearance, an
    // aria-modal overlay lands on screen with focus still on the page behind it --
    // a keyboard user is left tabbing through controls the overlay covers.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    await waitFor(() => {
      expect((dialog() as HTMLElement).contains(document.activeElement)).toBe(true)
    })
  })

  it('keeps an open share card mounted when the policy is revoked mid-share', async () => {
    // The card guards ITSELF: it re-reads permission from a ref after its export
    // await, and that ref only refreshes while the card is still rendering. Pulling
    // the card out on a policy flip freezes the ref at `true`, so an export already
    // in flight goes on to open the social composer -- the exact navigation the
    // revocation was meant to stop. So the card stays, and is handed the new answer.
    mockedApi.featureVideoNext.mockResolvedValue({ video: clip, enabled: true })
    const { rerender } = await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())

    rerender(<StartupVideoModal shareEnabled={false} onClose={vi.fn()} />)
    await act(async () => { await new Promise(r => setTimeout(r, 5)) })

    expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument()
    expect(screen.getByTestId('stub-share-enabled')).toHaveTextContent('false')
    // The ENTRY still fails closed: no new share can be started under a policy
    // that now says no.
    expect(screen.queryByTestId('startup-video-share')).not.toBeInTheDocument()
  })

  it('closes without a verdict and journals the failure when the clip will not load', async () => {
    // A catalog entry whose asset is missing, or a codec this browser cannot play.
    // Leaving an empty player on screen is worse than showing nothing, and writing
    // `dismissed` would retire permanently a clip the user never actually saw.
    __resetErrorJournalForTests()
    const { onClose } = await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())

    fireEvent.error(video() as HTMLVideoElement)

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    // No verdict, either kind: the clip is offered again next launch.
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
    // Journaled, not logged -- the browser fetches `src` itself, so the api
    // client's own error path never sees this request.
    const report = findReport(i18nT('components.startupVideoModal.media_failed'))
    expect(report).toBeDefined()
    expect(report?.endpoint).toBe(clip.src)
    expect(report?.source).toBe('api')
  })

  it('holds a fixed box with a background so a missing poster cannot collapse it', async () => {
    // There is no `onError` for a poster. The player keeps its own aspect box and
    // an opaque background, so a 404 poster leaves a plain panel rather than a
    // zero-height gap that shifts everything under it.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    expect(el.className).toContain('aspect-video')
    expect(el.className).toContain('bg-bg')
  })

  it('sends the active slot key on both the metadata read and the verdict write', async () => {
    // The server's own incognito/temporary guard reads X-Session-Key, and it treats
    // the shared `dashboard:ui` default as NOT restricted. Omitting the key makes
    // that guard unreachable, leaving the dashboard's client-side gate as the only
    // thing between a session that keeps nothing and a PERMANENT verdict.
    const store = createTestStore()
    store.dispatch(setActiveSlot('slot-7'))
    const onClose = vi.fn()
    renderWithProviders(<StartupVideoModal onClose={onClose} />, { store })
    for (let i = 0; i < 6; i++) {
      await act(async () => { await new Promise(r => setTimeout(r, 5)) })
    }
    await waitFor(() => expect(dialog()).toBeInTheDocument())

    expect(mockedApi.featureVideoNext).toHaveBeenCalledWith('dashboard:slot-7')

    fireEvent.click(screen.getByText(i18nT('components.startupVideoModal.got_it')))
    await waitFor(() => expect(mockedApi.featureVideoFeedback).toHaveBeenCalled())
    expect(mockedApi.featureVideoFeedback).toHaveBeenCalledWith(
      clip.id, 'seen', 'dashboard:slot-7',
    )
  })

  it('sends no session key when no slot is active', async () => {
    // `dashboard:` with nothing after it would name a slot that does not exist, so
    // the key is omitted entirely and the server falls back to its own default.
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(mockedApi.featureVideoNext).toHaveBeenCalledWith(undefined)

    fireEvent.click(screen.getByText(i18nT('components.startupVideoModal.got_it')))
    await waitFor(() => expect(mockedApi.featureVideoFeedback).toHaveBeenCalled())
    expect(mockedApi.featureVideoFeedback).toHaveBeenCalledWith(clip.id, 'seen', undefined)
    expect(onClose).toHaveBeenCalled()
  })

  it('does not wear the changelog popup\'s title', async () => {
    // Both are centre-screen startup popups, and they alternate across launches, so
    // sharing one name left a user unable to tell the release notes from the video.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const header = i18nT('components.startupVideoModal.feature_intro')
    expect(screen.getByText(header)).toBeInTheDocument()
    expect(header).not.toBe(i18nT('app.what_s_new'))
  })

  it('never fetches a cached clip\'s bytes before the user asks for them', async () => {
    // The whole cost argument for showing this at startup: the poster is drawn
    // and the media waits. For a clip on disk `preload` must be "none" and there
    // must be no autoplay, or every launch reads a video nobody watched.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    expect(video()).toHaveAttribute('preload', 'none')
    expect(video()).not.toHaveAttribute('autoplay')
    expect(video()).toHaveAttribute('controls')
  })

  it('does not render a video element at all while there is nothing to show', async () => {
    // The strongest form of "no fetch before open": no element exists to fetch
    // with, so a disabled or empty response cannot touch the network.
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    expect(video()).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.startupVideoModal.close') }))
      .not.toBeInTheDocument()
  })
})

describe('StartupVideoModal — verdicts are permanent', () => {
  it('posts `seen` at 80% and KEEPS PLAYING — the dialog does not close itself', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    // happy-dom does not decode media, so `duration` never resolves; the component
    // falls back to the catalog's `duration_s`, which is the same path a stream with
    // unknown duration takes.
    el.currentTime = 7.9 // 79% — under the line
    fireEvent.timeUpdate(el)
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()

    el.currentTime = 8 // exactly 80%
    fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'seen', undefined))

    // The regression guard. Crossing the threshold is a fact about the clip, not a
    // request to take it off screen: closing here snatched the dialog away
    // mid-playback, and because `seen` is permanent the last 20% became unwatchable
    // forever. An earlier version of this test asserted the opposite and so defended
    // the bug.
    expect(onClose).not.toHaveBeenCalled()
    expect(dialog()).toBeInTheDocument()
    expect(video()).toBeInTheDocument()
  })

  it('closes when the clip reaches its end, without posting a second verdict', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    el.currentTime = 8
    fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1))

    fireEvent.ended(el)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    // The 80% mark already recorded it; finishing must not post again.
    expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1)
  })

  it('posts exactly one verdict however many timeupdates arrive', async () => {
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    el.currentTime = 9
    // `timeupdate` fires several times a second; a per-event post would record
    // the same decision dozens of times.
    for (let i = 0; i < 5; i++) fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1))
  })

  it('posts `seen` when the acknowledgement is pressed', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.startupVideoModal.got_it') }))
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'seen', undefined))
    expect(onClose).toHaveBeenCalled()
  })

  it('posts `dismissed` when closed with the X', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.startupVideoModal.close') }))
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'dismissed', undefined))
    expect(onClose).toHaveBeenCalled()
  })

  it('posts `dismissed` when closed with Escape', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'dismissed', undefined))
  })

  it('closes WITHOUT a verdict when the backdrop is clicked', async () => {
    // A stray click on the scrim is not a decision about the clip, and `dismissed` is
    // permanent -- one misclick used to retire a clip the user never watched, with no
    // re-watch path. Closing silently leaves the verdict unwritten so the backend
    // offers it again next launch. An earlier version of this test asserted the
    // opposite and pinned the behaviour that was overturned.
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const scrim = document.querySelector('[role="presentation"]') as HTMLElement
    fireEvent.click(scrim)

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })

  it('does not undo a verdict already recorded at 80% when the backdrop closes it', async () => {
    // Closing without a verdict declines to write a NEW one; it must not retract the
    // `seen` the threshold already sent.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    el.currentTime = 8
    fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'seen', undefined))

    fireEvent.click(document.querySelector('[role="presentation"]') as HTMLElement)
    // Still exactly the one `seen`: no second call, and no `dismissed` overwriting it.
    expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1)
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalledWith(clip.id, 'dismissed', undefined)
  })

  it('still posts `dismissed` for the deliberate closes — X and Escape', async () => {
    // The distinction the fix rests on: a stray scrim click is not a decision, while
    // pressing X or Escape is. Those keep recording.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'dismissed', undefined))
  })

  it('does not dismiss when a click lands INSIDE the dialog', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(dialog() as HTMLElement)
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })

  it('closes even when the verdict write fails', async () => {
    // The write is fire-and-forget on purpose: a slow or broken gateway must not
    // leave the user trapped behind a dialog they have finished with.
    mockedApi.featureVideoFeedback.mockRejectedValue(new Error('gateway down'))
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.startupVideoModal.got_it') }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })
})

describe('StartupVideoModal — share governance fails closed', () => {
  const shareLabel = () => i18nT('components.startupVideoModal.share')

  /** Nothing on the page that could reach an X or LinkedIn intent URL. */
  function expectNoIntentSurface() {
    expect(screen.queryByTestId('startup-video-share')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: shareLabel() })).not.toBeInTheDocument()
    expect(screen.queryByTestId('stub-share-modal')).not.toBeInTheDocument()
    expect(screen.queryByTestId('share-x')).not.toBeInTheDocument()
    expect(screen.queryByTestId('share-linkedin')).not.toBeInTheDocument()
  }

  it('hides the whole share section when the prop is ABSENT', async () => {
    // A forgotten wire must hide sharing, not expose it — the prop defaults to
    // false for exactly this case.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expectNoIntentSurface()
  })

  it('hides the whole share section when governance says false', async () => {
    // `social_share_enabled: false` from /api/dashboard/config. Hidden, NOT
    // disabled: a greyed button is still an element that explains a policy the
    // user cannot act on.
    await mount({ shareEnabled: false })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expectNoIntentSurface()
  })

  it('renders no disabled share control either', async () => {
    await mount({ shareEnabled: false })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    // Guards against a refactor that "keeps the affordance visible" by disabling
    // it: with the policy off there must be no share control in any state.
    const disabled = screen.queryAllByRole('button').filter(b => b.hasAttribute('disabled'))
    expect(disabled).toHaveLength(0)
    expect(document.body.textContent).not.toContain(shareLabel())
  })

  it('offers the share entry only once governance says true', async () => {
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('startup-video-share')).toBeInTheDocument()
    // Still nothing that reaches an intent until the user opens the card.
    expect(screen.queryByTestId('share-x')).not.toBeInTheDocument()
  })

  it('shares the clip title and its blurb through the existing card', async () => {
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    expect(screen.getByTestId('stub-share-title')).toHaveTextContent(clip.title)
    expect(screen.getByTestId('stub-share-body')).toHaveTextContent(clip.description)
  })

  it('puts the RESOLVED docs URL in the share caption, never the raw filename', async () => {
    // `doc` is a bare filename in the catalog, so posting it raw put a string nobody
    // can open into a public feed. `tipDocHref` is the shipping resolver for that
    // same field, so the caption carries a real link a reader can follow.
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    const text = screen.getByTestId('stub-share-body').textContent ?? ''
    expect(text).toContain(clip.description)
    expect(text).toContain(tipDocHref(clip.doc) as string)
    expect(text).toContain('https://')
    // The raw filename never appears on its own -- only inside the resolved URL.
    expect(text.replace(tipDocHref(clip.doc) as string, '')).not.toContain(clip.doc as string)
  })

  it('hands the composers a caption with the title, blurb and docs link -- not the chat sentence', async () => {
    // `messageText` is only the card image. What X / LinkedIn and the clipboard
    // receive is the card's `caption`, which defaults to "<product> just did this
    // for me" -- a sentence about a reply the assistant wrote. A feature clip did
    // nothing for anyone, so the post text has to be supplied by this host.
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    const caption = screen.getByTestId('stub-share-copy-caption').textContent ?? ''
    expect(caption).toContain(clip.title)
    expect(caption).toContain(clip.description)
    expect(caption).toContain(tipDocHref(clip.doc) as string)
    expect(caption).not.toContain('did this for me')
  })

  it('drops the docs link when the catalog entry is not a plain filename', async () => {
    // `tipDocHref` refuses anything that is not a `*.md` filename and returns null,
    // so a bad entry costs the link rather than pasting something unopenable.
    mockedApi.featureVideoNext.mockResolvedValue({
      video: { ...clip, doc: 'https://evil.invalid/x' }, enabled: true,
    } as never)
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    const text = screen.getByTestId('stub-share-body').textContent ?? ''
    expect(text).toBe(clip.description)
    expect(text).not.toContain('evil.invalid')
  })

  it('gives the share card video wording instead of the chat defaults', async () => {
    // The reused card describes a chat reply and a question by default. Sharing a
    // feature clip through it unchanged made the dialog assert something false.
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())

    expect(screen.getByTestId('stub-share-copy-description'))
      .toHaveTextContent(i18nT('components.startupVideoModal.share_description'))
    expect(screen.getByTestId('stub-share-copy-include'))
      .toHaveTextContent(i18nT('components.startupVideoModal.share_include_title'))
    // And the chat defaults must not leak through.
    expect(document.body.textContent).not.toContain(i18nT('pages.chat.share.description'))
    expect(document.body.textContent).not.toContain(i18nT('pages.chat.share.include_question'))
  })

  it('opening the share card does not retire the clip', async () => {
    // Sharing is not an acknowledgement: the user may still watch it afterwards.
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })
})
