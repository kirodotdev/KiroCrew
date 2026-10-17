/**
 * "Test microphone" strip under the Speech-to-Text microphone picker.
 *
 * Opens the selected input (or the system default) and shows its live level, so
 * a user can tell a working microphone from a muted, wrong or silent one before
 * the first dictation. The audio goes to an AnalyserNode and nowhere else: no
 * recorder, no upload, no transcription, and the analyser is never connected to
 * the speakers, so the test cannot echo.
 *
 * Capture is released on Stop, when the picked device changes (the test
 * restarts on the new one), and on unmount, which covers leaving Settings.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { CircleStop, Mic } from 'lucide-react'

import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { acquireMicStream, createLevelMeter, humanizeMicError, stripDefaultPrefix } from '../hooks/mic'
import { i18nT } from '../i18n/t'

interface Props {
  /** The picker's value: a deviceId, or `''` for the system default. */
  deviceId: string
  /** Called after a test opened a device, so the picker can re-read labels
   *  that only appear once the page holds mic permission. */
  onDevicesMayHaveChanged?: () => void
  /** Whether an error may offer the "ask the agent" hand-off, which navigates
   *  away. The host turns it off while a sibling field holds an unsaved draft. */
  askAgent?: boolean
}

type State =
  | { kind: 'idle' }
  | { kind: 'starting' }
  | { kind: 'live'; label: string }
  | { kind: 'error'; message: string; handOff: boolean }

export function MicTestStrip({ deviceId, onDevicesMayHaveChanged, askAgent = true }: Props) {
  const [state, setState] = useState<State>({ kind: 'idle' })
  const [level, setLevel] = useState(0)
  // One live capture at most. `run` invalidates a start that is still waiting
  // on getUserMedia when Stop, a device switch or unmount gets there first.
  const releaseRef = useRef<(() => void) | null>(null)
  const runRef = useRef(0)
  const notifyRef = useRef(onDevicesMayHaveChanged)
  notifyRef.current = onDevicesMayHaveChanged

  const release = useCallback(() => {
    runRef.current += 1
    releaseRef.current?.()
    releaseRef.current = null
  }, [])

  const start = useCallback(async (id: string) => {
    release()
    const run = runRef.current
    setState({ kind: 'starting' })
    let stream: MediaStream
    try {
      // Explicit pick: `exact`, no silent fallback, so the meter is the device
      // the user chose or a visible error.
      stream = await acquireMicStream(id)
    } catch (e) {
      if (run === runRef.current) setState({ kind: 'error', message: humanizeMicError(e), handOff: true })
      return
    }
    if (run !== runRef.current) {
      stream.getTracks().forEach(t => t.stop())
      return
    }
    const stopMeter = createLevelMeter(stream, setLevel)
    const track = stream.getAudioTracks()[0]
    // The device went away mid-test (unplugged, or taken by the OS). Without
    // this the strip kept saying "Listening on <device>" over a flat meter.
    const onEnded = () => {
      if (run !== runRef.current) return
      release()
      setLevel(0)
      // No agent hand-off: the sentence names the fix, and the picker that
      // applies it is right above, where the hand-off would navigate away from.
      setState({ kind: 'error', message: i18nT('components.micTestStrip.disconnected'), handOff: false })
    }
    track?.addEventListener?.('ended', onEnded)
    releaseRef.current = () => {
      track?.removeEventListener?.('ended', onEnded)
      stopMeter()
      stream.getTracks().forEach(t => t.stop())
    }
    // Same name the picker shows: Chromium labels the default track
    // "Default - <name>", which the picker already strips.
    setState({ kind: 'live', label: stripDefaultPrefix(track?.label || '') })
    notifyRef.current?.()
  }, [release])

  const stop = useCallback(() => {
    release()
    setLevel(0)
    setState({ kind: 'idle' })
  }, [release])

  // Follow the picker while a test runs; an idle strip stays idle.
  const testingRef = useRef(false)
  testingRef.current = state.kind === 'live' || state.kind === 'starting'
  // An error belongs to the device it came from, so a new pick clears it.
  useEffect(() => {
    if (testingRef.current) void start(deviceId)
    else setState(s => (s.kind === 'error' ? { kind: 'idle' } : s))
  }, [deviceId, start])

  useEffect(() => release, [release])

  const testing = state.kind === 'live' || state.kind === 'starting'
  const percent = Math.round(level * 100)
  return (
    <div className="-mt-1 mb-1 flex flex-col gap-1.5" data-testid="mic-test">
      <div className="flex flex-wrap items-center gap-2">
        {testing ? (
          <Btn onClick={stop}>
            <CircleStop className="lucide-inline" /> {i18nT('components.micTestStrip.stop')}
          </Btn>
        ) : (
          <Btn onClick={() => void start(deviceId)}>
            <Mic className="lucide-inline" /> {i18nT('components.micTestStrip.test')}
          </Btn>
        )}
        {/* Live only: an empty track while the device is still opening reads
            as a silent microphone. */}
        {state.kind === 'live' && (
          <div
            role="meter"
            aria-label={i18nT('components.micTestStrip.level')}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="h-2 w-40 bg-border rounded-full overflow-hidden"
          >
            <div
              className="h-full bg-accent rounded-full transition-[width] duration-75 motion-reduce:transition-none"
              style={{ width: `${percent}%` }}
            />
          </div>
        )}
      </div>
      {/* One status line, always rendered, so starting or stopping a test does
          not move every field below it. An error takes the same slot. The
          hand-off is the host's call: this strip holds no draft and the picker
          commits on change, but the hand-off navigates away from the whole
          card, so SttSettings turns it off while its AWS profile/region fields
          hold an unsaved value. */}
      {state.kind === 'error' ? (
        <ErrorNotice variant="inline" message={state.message} askAgent={askAgent && state.handOff} />
      ) : (
        <p className="text-[12px] text-muted" aria-live="polite">
          {state.kind === 'live'
            ? state.label
              ? i18nT('components.micTestStrip.listening_on', { device: state.label })
              : i18nT('components.micTestStrip.listening')
            : state.kind === 'starting'
              // The browser's permission prompt can be open now; say something
              // is waiting rather than showing the idle hint under "Stop test".
              ? i18nT('components.micTestStrip.opening')
              : i18nT('components.micTestStrip.idle_hint')}
        </p>
      )}
    </div>
  )
}
