import { lazy, Suspense, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAppSelector } from '../../store'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import type { UpdateState } from '../../hooks/useUpdateSubscription'
import {
  canShowStartupVideo,
  markStartupVideoHandled,
  startupVideoHandledThisLaunch,
} from '../../components/startupVideoGate'

// Lazy, like the update-found popup `App.tsx` mounts: the startup feature clip pulls in
// a video element and the whole share-card graph, and most launches never show
// it. The policy that decides whether this chunk is ever fetched lives in
// `startupVideoGate`, which is eagerly imported and tiny.
const StartupVideoModal = lazy(() => import('../../components/StartupVideoModal'))

/** Every launch-time interruption the startup video yields to, read in the same render. */
interface StartupVideoInputs {
  showChangelog: boolean
  changelogDecided: boolean
  updateAvailable: boolean
  showOnboarding: boolean
  showAgentImport: boolean
  showPrivacy: boolean
  importOnboarded: boolean
  privacyAcked: boolean
  onboarded: boolean
  themeBootReady: boolean
}

export function useStartupVideo({
  showChangelog, changelogDecided, updateAvailable, showOnboarding, showAgentImport, showPrivacy,
  importOnboarded, privacyAcked, onboarded, themeBootReady,
}: StartupVideoInputs) {
  // ---------------------------------------------------------------- Startup
  // feature-intro video. Sequencing policy lives in `startupVideoGate`; this is
  // the wiring that feeds it and the two pieces of state it drives.

  // Whether UpdateModal is claiming the screen. It self-gates on the shared
  // ['update-state'] cache rather than on a prop, so the only honest way to ask
  // is to read the same cache with the same condition it uses.
  const { data: desktopUpdateState } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false, // populated by useUpdateSubscription, which `App.tsx` calls
    staleTime: Infinity,
  })
  const updateStaged = !!desktopUpdateState
    && desktopUpdateState.state === 'downloaded'
    && !desktopUpdateState.replayed

  // Governance for the share entry: the SAME query key and the same `=== true`
  // test the chat surface uses, so one policy answer drives both and a
  // mid-session swap invalidates both at once. No new scope, no new flag.
  const { data: startupShareCfg } = useQuery<{ social_share_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    staleTime: 30_000,
  })
  const socialShareOn = startupShareCfg?.social_share_enabled === true

  // A verdict is a durable per-user write, so a session that keeps nothing does
  // not get asked. Read off the ACTIVE slot, matching where `memory_mode` is
  // authoritative everywhere else.
  const activeSlotMemoryMode = useAppSelector(
    s => s.dashboard.slots.find(x => x.key === s.chat.activeSlot)?.memory_mode,
  )
  // The slot list is filled by a fetch that lands AFTER mount. Until it does, the
  // active slot resolves to nothing and `activeSlotMemoryMode` is undefined --
  // which reads as "not incognito" and is the wrong answer to act on. This flag is
  // the store's own record that the list is authoritative.
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)

  // Is any part of first-run still owed? Read from the AUTHORITATIVE flags rather
  // than from `showOnboarding` / `showAgentImport` / `showPrivacy`, which an effect
  // sets. In the commit that flips `themeBootReady` that effect has only SCHEDULED
  // its update, so those three still read false while first-run is about to claim
  // the launch -- and the video would open beside Agent Import on a brand-new
  // install's first screen. These three are what that effect derives from, so they
  // are already correct in the same commit.
  const onboardingOwed = !importOnboarded || !privacyAcked || !onboarded

  // Latched, not sampled: an interruption that has already been dismissed still
  // spends the launch. Sampling would let the video open the instant the user
  // closed the changelog, which is the back-to-back pair the policy forbids.
  const [startupInterruptionSeen, setStartupInterruptionSeen] = useState(false)
  // The LIVE reading of the same conditions the latch is fed from. The gate reads
  // both, and the live one is load-bearing: the latch is written by the effect
  // below, which runs AFTER the commit that showed the changelog, while
  // `changelogDecided` is set one microtask later on the same fetch chain. When
  // that microtask lands between the commit and its passive effects, the gate's
  // own effect runs in a render where `changelogDecided` is already true and the
  // latch still false: and opened the video beside the changelog (flaked in 2
  // of 5 frontend runs). Same shape as `onboardingOwed` above: derive from the
  // authoritative flags in the same commit, keep the latch for after they clear.
  const startupInterruptionLive = showChangelog || updateAvailable || updateStaged
    || showOnboarding || showAgentImport || showPrivacy
  useEffect(() => {
    if (startupInterruptionLive) {
      setStartupInterruptionSeen(true)
    }
  }, [startupInterruptionLive])

  const [startupVideoOpen, setStartupVideoOpen] = useState(false)
  const [startupVideoDone, setStartupVideoDone] = useState(false)
  // The gate is consulted ONLY while the modal is closed, and the decision is
  // latched into state. Re-evaluating it against a live condition would let a
  // late-arriving update notice unmount a clip the user is in the middle of
  // watching — worse than the collision the policy is protecting against.
  useEffect(() => {
    if (startupVideoOpen || startupVideoDone) return
    if (!canShowStartupVideo({
      // Either an interruption already appeared this launch, or first-run is still
      // owed and is about to. Both spend the launch.
      interruptionShown: startupInterruptionSeen || startupInterruptionLive || onboardingOwed,
      // Three separate authorities, and the video waits for ALL of them: onboarding's
      // three modals are decided by the `themeBootReady` effect (`firstRun.tsx`), the changelog
      // decides across its own fetch, and the slot list decides whether this session
      // keeps anything. An absent answer from any of them is not a negative one.
      settled: themeBootReady && changelogDecided && slotsLoaded,
      memoryMode: activeSlotMemoryMode,
      handledThisLaunch: startupVideoHandledThisLaunch(),
    })) return
    markStartupVideoHandled()
    setStartupVideoOpen(true)
  }, [
    startupVideoOpen, startupVideoDone, startupInterruptionSeen, startupInterruptionLive,
    onboardingOwed, themeBootReady, changelogDecided, slotsLoaded, activeSlotMemoryMode,
  ])
  return { startupVideoOpen, startupVideoDone, setStartupVideoDone, socialShareOn }
}

export function StartupVideo({ startupVideo }: { startupVideo: ReturnType<typeof useStartupVideo> }) {
  const { startupVideoOpen, startupVideoDone, setStartupVideoDone, socialShareOn } = startupVideo
  if (!(startupVideoOpen && !startupVideoDone)) return null
  return (
    <Suspense fallback={null}>
      <StartupVideoModal
        shareEnabled={socialShareOn}
        onClose={() => setStartupVideoDone(true)}
      />
    </Suspense>
  )
}
