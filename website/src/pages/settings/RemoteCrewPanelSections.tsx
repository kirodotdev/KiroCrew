/**
 * Settings -> Remote Crew: the page's sections and the Add Crew modal, which
 * `RemoteCrewPanel.tsx` composes.
 *
 * - `CrewSection` is one labelled region ("My Crews", "Settings"); the Shared
 *   Crews card draws its own, since it is hidden when the relay is off.
 * - `AddCrewModal` asks how to add a Crew, then shows the form for that choice.
 *   It draws the forms it is handed and owns none of their state: the panel and
 *   the store keep it, so closing the modal and opening it again loses nothing.
 * - `RemoteCrewSettingsSection` groups the page-wide settings.
 */
import type { ReactNode } from 'react'
import { ArrowLeft, Rocket, Server } from 'lucide-react'
import type { InstanceView } from '../../api/client'
import { Btn, Card, PanelSectionHeader } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import Modal from '../../components/Modal'
import DefaultDestinationCrewPicker from '../../components/DefaultDestinationCrewPicker'
import { AUTO_CONNECT_KEY } from '../../hooks/useAutoConnectInstances'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { i18nT } from '../../i18n/t'

/** A labelled region of the page: its header (label, count, any controls) over its content. */
export function CrewSection({ label, count, trailing, children, testId }: {
  label: string
  count?: number
  trailing?: ReactNode
  children: ReactNode
  testId?: string
}) {
  return (
    <section aria-label={label} data-testid={testId} className="space-y-2">
      <PanelSectionHeader label={label} count={count} trailing={trailing} />
      {children}
    </section>
  )
}

/** Which step of Add Crew is on screen. */
export type AddCrewMode = 'choose' | 'setup' | 'connect'

function AddChoice({ icon, title, desc, onPick }: { icon: ReactNode; title: string; desc: string; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      className="w-full flex items-start gap-3 text-left rounded-lg border border-border px-3.5 py-3 hover:bg-bg-hover focus-ring"
    >
      <span className="mt-0.5 w-8 h-8 shrink-0 grid place-items-center rounded-md bg-accent-subtle text-accent" aria-hidden>{icon}</span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-text-strong">{title}</span>
        <span className="block text-[12px] text-muted">{desc}</span>
      </span>
    </button>
  )
}

export function AddCrewModal({ mode, onMode, onClose, setup, connect }: {
  mode: AddCrewMode
  onMode: (mode: AddCrewMode) => void
  onClose: () => void
  /** Today's launch checklist, form and progress card. */
  setup: ReactNode
  /** Today's add-a-machine form. */
  connect: ReactNode
}) {
  const choosing = mode === 'choose'
  return (
    <Modal
      open
      onClose={onClose}
      title={choosing
        ? i18nT('pages.settings.remoteCrewPanel.add_crew_title')
        : mode === 'setup'
          ? i18nT('pages.settings.remoteCrewPanel.add_choice_setup')
          : i18nT('pages.settings.remoteCrewPanel.add_choice_connect')}
      maxWidth={choosing ? 480 : 760}
      // The forms hold typed values; a stray click outside must not close them.
      guardAccidentalDismiss={!choosing}
      footer={(
        // The X in the header closes every step; the footer steps back or cancels.
        <div className="flex items-center justify-between gap-2">
          {choosing ? <span /> : (
            <Btn onClick={() => onMode('choose')}>
              <ArrowLeft className="lucide-inline" /> {i18nT('app.nav_back')}
            </Btn>
          )}
          {choosing && <Btn onClick={onClose}>{i18nT('pages.settings.remoteCrewPanel.cancel')}</Btn>}
        </div>
      )}
    >
      {choosing ? (
        <div className="space-y-2.5" data-testid="add-crew-choices">
          <p className="text-[13px] text-muted">{i18nT('pages.settings.remoteCrewPanel.add_crew_desc')}</p>
          <AddChoice
            icon={<Rocket size={16} />}
            title={i18nT('pages.settings.remoteCrewPanel.add_choice_setup')}
            desc={i18nT('pages.settings.remoteCrewPanel.add_choice_setup_desc')}
            onPick={() => onMode('setup')}
          />
          <AddChoice
            icon={<Server size={16} />}
            title={i18nT('pages.settings.remoteCrewPanel.add_choice_connect')}
            desc={i18nT('pages.settings.remoteCrewPanel.add_choice_connect_desc')}
            onPick={() => onMode('connect')}
          />
        </div>
      ) : mode === 'setup' ? setup : connect}
    </Modal>
  )
}

/** "Auto-connect crews" and the default destination crew, the page's two settings. */
export function RemoteCrewSettingsSection({ instances, defaultDestination }: {
  instances: readonly InstanceView[]
  defaultDestination: string
}) {
  // Default-on: when set, the web app auto-connects every crew on load and on
  // tab focus (see useAutoConnectInstances). Off lets a many-crew user stop the
  // per-load SSH + token-mint fan-out.
  const [autoConnect, setAutoConnect] = usePersistedBool(AUTO_CONNECT_KEY, true)
  return (
    <CrewSection label={i18nT('pages.settings.remoteCrewPanel.settings_section')} testId="remote-crew-settings">
      <Card className="divide-y divide-border">
        <SettingsToggle
          label={i18nT('pages.settings.remoteCrewPanel.auto_connect')}
          hint={i18nT('pages.settings.remoteCrewPanel.auto_connect_desc')}
          checked={autoConnect}
          onChange={setAutoConnect}
        />
        <DefaultDestinationCrewPicker instances={instances} value={defaultDestination} />
      </Card>
    </CrewSection>
  )
}
