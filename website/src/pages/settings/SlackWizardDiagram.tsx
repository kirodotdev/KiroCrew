/**
 * Schematic pictures for the Slack setup wizard: where to click on Slack's
 * pages and in the Slack client, as simplified frames with the target outlined
 * (dashed) and numbered in click order. Not screenshots: Slack's layout changes
 * and its screens are not ours to ship, so each picture names only what the
 * step's text names. Drawn with positioned elements rather than inline SVG (the
 * repo's use-lucide-icons rule), so every label is ordinary translated text.
 *
 * Colours come from the theme variables, so every theme (dark included) reads.
 * Every word goes through the catalog. Slack's own button and page names keep
 * Slack's English spelling in every locale (api.slack.com is English-only, and
 * the user has to find exactly that word); non-Latin locales add a short role
 * word such as "button" beside it.
 */
import type { CSSProperties, ReactNode } from 'react'
import { Plus } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { APP_TOKEN_SCOPE, type WizardStep } from './SlackSetupWizard'

/** The drawing's own coordinate space. Positions are given in these units and
 *  become percentages, so the picture scales with its container. */
const W = 360
const H = 170
/** Narrowest rendered width, in px: the smallest label (9.5 units) is then
 *  9.5 * 384 / 360 ≈ 10.1px, above the dashboard's 10px floor. */
const MIN_WIDTH = 384
/** A length in drawing units, relative to the picture's width (it is a size container). */
const u = (n: number) => `${(n / W) * 100}cqw`

const at = (x: number, y: number, w: number, h: number): CSSProperties => ({
  position: 'absolute',
  left: `${(x / W) * 100}%`,
  top: `${(y / H) * 100}%`,
  width: `${(w / W) * 100}%`,
  height: `${(h / H) * 100}%`,
})

type BoxProps = {
  x: number; y: number; w: number; h: number
  label?: string
  /** The thing to click: a dashed accent outline, so it never reads as a
   *  button of this page. */
  hl?: boolean
  /** A numbered badge at the box's top-left corner: the click order. */
  n?: number
  mono?: boolean
  round?: number
  fill?: string
  border?: string
  children?: ReactNode
}

function Box({ x, y, w, h, label, hl, n, mono, round = 4, fill = 'var(--bg)', border = 'var(--border)', children }: BoxProps) {
  return (
    <div style={{
      ...at(x, y, w, h),
      boxSizing: 'border-box',
      borderRadius: u(round),
      background: hl ? 'var(--accent-subtle)' : fill,
      border: hl ? `${u(1.4)} dashed var(--accent)` : `${u(1)} solid ${border}`,
      display: 'flex',
      alignItems: 'center',
      paddingLeft: u(6),
      fontSize: u(9.5),
      lineHeight: 1,
      whiteSpace: 'nowrap',
      fontFamily: mono ? 'ui-monospace, monospace' : undefined,
      color: hl ? 'var(--text)' : 'var(--muted)',
    }}>
      {label}
      {children}
      {n !== undefined && <Badge n={n} />}
    </div>
  )
}

function Badge({ n }: { n: number }) {
  return (
    <span style={{
      position: 'absolute', left: u(-7.5), top: u(-7.5), width: u(15), height: u(15),
      borderRadius: '50%', background: 'var(--accent)', color: 'var(--bg)',
      fontSize: u(9.5), fontWeight: 700, lineHeight: 1,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>{n}</span>
  )
}

/** Plain text with its top-left corner at (x, y). */
function Label({ x, y, text, strong }: { x: number; y: number; text: string; strong?: boolean }) {
  return (
    <span style={{
      position: 'absolute', left: `${(x / W) * 100}%`, top: `${(y / H) * 100}%`,
      fontSize: u(strong ? 10.5 : 9.5), fontWeight: strong ? 600 : 400, lineHeight: 1, whiteSpace: 'nowrap',
      color: strong ? 'var(--text)' : 'var(--muted)',
    }}>{text}</span>
  )
}

/** A plain panel: no label, never the click target. */
function Panel({ x, y, w, h, raised }: { x: number; y: number; w: number; h: number; raised?: boolean }) {
  return (
    <Box x={x} y={y} w={w} h={h} round={raised ? 6 : 4}
      fill={raised ? 'var(--bg-elevated)' : 'var(--bg)'}
      border={raised ? 'var(--border-strong)' : 'var(--border)'} />
  )
}

/** A browser-ish window: api.slack.com pages and the Slack client share it. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <>
      <Box x={0} y={0} w={W} h={H} round={8} fill="var(--bg-elevated)" />
      <div style={{ ...at(0, 16, W, 0), borderTop: `${u(1)} solid var(--border)` }} />
      {[10, 20, 30].map(cx => (
        <span key={cx} style={{
          position: 'absolute', left: `${((cx - 2.5) / W) * 100}%`, top: `${(6 / H) * 100}%`,
          width: u(5), height: u(5), borderRadius: '50%', background: 'var(--border-strong)',
        }} />
      ))}
      {children}
    </>
  )
}

/** Every label, spelled out so each key is a literal the catalog checks see. */
function labels() {
  return {
    add_scope: i18nT('pages.settings.slackWizard.diagram.add_scope'),
    add_workspace: i18nT('pages.settings.slackWizard.diagram.add_workspace'),
    app_home: i18nT('pages.settings.slackWizard.diagram.app_home'),
    app_level_tokens: i18nT('pages.settings.slackWizard.diagram.app_level_tokens'),
    apps: i18nT('pages.settings.slackWizard.diagram.apps'),
    basic_information: i18nT('pages.settings.slackWizard.diagram.basic_information'),
    bot_user_oauth_token: i18nT('pages.settings.slackWizard.diagram.bot_user_oauth_token'),
    connections_write: i18nT('pages.settings.slackWizard.diagram.connections_write', { scope: APP_TOKEN_SCOPE }),
    copy: i18nT('pages.settings.slackWizard.diagram.copy'),
    copy_member_id: i18nT('pages.settings.slackWizard.diagram.copy_member_id'),
    create: i18nT('pages.settings.slackWizard.diagram.create'),
    generate: i18nT('pages.settings.slackWizard.diagram.generate'),
    generate_token_and_scopes: i18nT('pages.settings.slackWizard.diagram.generate_token_and_scopes'),
    install_to_workspace: i18nT('pages.settings.slackWizard.diagram.install_to_workspace'),
    more: i18nT('pages.settings.slackWizard.diagram.more'),
    next: i18nT('pages.settings.slackWizard.diagram.next'),
    oauth_permissions: i18nT('pages.settings.slackWizard.diagram.oauth_permissions'),
    oauth_tokens: i18nT('pages.settings.slackWizard.diagram.oauth_tokens'),
    profile: i18nT('pages.settings.slackWizard.diagram.profile'),
    sign_in_another: i18nT('pages.settings.slackWizard.diagram.sign_in_another'),
    socket_mode: i18nT('pages.settings.slackWizard.diagram.socket_mode'),
    view_files: i18nT('pages.settings.slackWizard.diagram.view_files'),
    xoxb_value: i18nT('pages.settings.slackWizard.diagram.xoxb_value'),
    bot_reply: i18nT('pages.settings.slackWizard.diagram.bot_reply'),
    manifest_preview: i18nT('pages.settings.slackWizard.diagram.manifest_preview'),
    message_box: i18nT('pages.settings.slackWizard.diagram.message_box'),
    pick_workspace: i18nT('pages.settings.slackWizard.diagram.pick_workspace'),
    workspace_dropdown: i18nT('pages.settings.slackWizard.diagram.workspace_dropdown'),
    your_app: i18nT('pages.settings.slackWizard.diagram.your_app'),
    your_message: i18nT('pages.settings.slackWizard.diagram.your_message'),
  }
}
type Labels = ReturnType<typeof labels>

/** The left menu of an app's page on api.slack.com. */
function AppSidebar({ active, n, d }: { active: 'basic_information' | 'oauth_permissions'; n: number; d: Labels }) {
  const items = [
    ['basic_information', d.basic_information],
    ['socket_mode', d.socket_mode],
    ['oauth_permissions', d.oauth_permissions],
    ['app_home', d.app_home],
  ] as const
  return (
    <>
      <Panel x={8} y={24} w={118} h={138} />
      {items.map(([id, label], i) => (
        <Box key={id} x={14} y={32 + i * 22} w={106} h={18} label={label}
          hl={id === active} n={id === active ? n : undefined} />
      ))}
    </>
  )
}

/** The Slack client: workspace rail, channel sidebar, message pane. */
function ClientShell({ children }: { children?: ReactNode }) {
  return (
    <>
      <Panel x={6} y={22} w={24} h={142} />
      <Box x={11} y={28} w={14} h={14} round={3} fill="var(--border-strong)" />
      <Panel x={34} y={22} w={98} h={142} />
      {children}
    </>
  )
}

function WorkspaceDiagram({ d }: { d: Labels }) {
  return (
    <ClientShell>
      <Box x={11} y={48} w={14} h={14} round={3} hl n={1}>
        <Plus aria-hidden style={{ position: 'absolute', inset: 0, margin: 'auto', width: '70%', height: '70%', color: 'var(--accent)' }} />
      </Box>
      <Panel x={40} y={46} w={170} h={52} raised />
      <Box x={46} y={52} w={158} h={18} label={d.add_workspace} hl n={2} />
      <Box x={46} y={74} w={158} h={18} label={d.sign_in_another} />
    </ClientShell>
  )
}

function CreateDiagram({ d }: { d: Labels }) {
  return (
    <>
      <Box x={50} y={26} w={260} h={134} round={6} border="var(--border-strong)" />
      <Label x={62} y={34} text={d.pick_workspace} strong />
      <Box x={62} y={54} w={236} h={22} label={d.workspace_dropdown} hl n={1} />
      <Box x={62} y={86} w={236} h={34} label={d.manifest_preview} />
      <Box x={166} y={130} w={56} h={20} label={d.next} hl n={2} />
      <Box x={232} y={130} w={66} h={20} label={d.create} hl n={3} />
    </>
  )
}

function BotTokenDiagram({ d }: { d: Labels }) {
  return (
    <>
      <AppSidebar active="oauth_permissions" n={1} d={d} />
      <Label x={134} y={30} text={d.oauth_tokens} strong />
      <Box x={134} y={50} w={150} h={22} label={d.install_to_workspace} hl n={2} />
      <Label x={134} y={86} text={d.bot_user_oauth_token} />
      <Box x={134} y={100} w={164} h={22} label={d.xoxb_value} mono />
      <Box x={304} y={100} w={48} h={22} label={d.copy} hl n={3} />
    </>
  )
}

function AppTokenDiagram({ d }: { d: Labels }) {
  return (
    <>
      <AppSidebar active="basic_information" n={1} d={d} />
      <Label x={134} y={30} text={d.app_level_tokens} strong />
      <Box x={134} y={50} w={180} h={22} label={d.generate_token_and_scopes} hl n={2} />
      <Panel x={160} y={80} w={192} h={82} raised />
      <Box x={170} y={90} w={172} h={20} label={d.add_scope} hl n={3} />
      <Box x={170} y={114} w={172} h={18} label={d.connections_write} mono />
      <Box x={276} y={138} w={66} h={18} label={d.generate} hl n={4} />
    </>
  )
}

function OwnerDiagram({ d }: { d: Labels }) {
  return (
    <ClientShell>
      <Box x={W - 30} y={22} w={16} h={16} round={8} hl n={1} />
      <Panel x={212} y={42} w={130} h={30} raised />
      <Box x={218} y={48} w={118} h={18} label={d.profile} hl n={2} />
      <Panel x={146} y={80} w={200} h={82} />
      <Box x={316} y={88} w={22} h={18} label={d.more} hl n={3} />
      <Panel x={176} y={110} w={162} h={44} raised />
      <Box x={182} y={116} w={150} h={16} label={d.view_files} />
      <Box x={182} y={135} w={150} h={16} label={d.copy_member_id} hl n={4} />
    </ClientShell>
  )
}

function TestDiagram({ d }: { d: Labels }) {
  return (
    <ClientShell>
      <Label x={42} y={32} text={d.apps} />
      <Box x={40} y={46} w={86} h={18} label={d.your_app} hl n={1} />
      <Panel x={140} y={22} w={212} h={142} />
      <Box x={150} y={34} w={150} h={22} label={d.your_message} round={8} />
      <Box x={150} y={64} w={170} h={22} label={d.bot_reply} round={8} hl n={3} />
      <Box x={150} y={134} w={192} h={22} label={d.message_box} hl n={2} />
    </ClientShell>
  )
}

/** The picture for one wizard step, captioned so it never reads as part of this page. */
export function SlackWizardDiagram({ step }: { step: WizardStep }) {
  const d = labels()
  let body: ReactNode
  switch (step) {
    case 'workspace': body = <WorkspaceDiagram d={d} />; break
    case 'create': body = <CreateDiagram d={d} />; break
    case 'bot_token': body = <BotTokenDiagram d={d} />; break
    case 'app_token': body = <AppTokenDiagram d={d} />; break
    case 'owner': body = <OwnerDiagram d={d} />; break
    case 'test': body = <TestDiagram d={d} />; break
  }
  return (
    <figure className="mt-3 mb-0 mx-0 w-full max-w-[460px]">
      <figcaption className="text-[11px] text-muted mb-1">{i18nT('pages.settings.slackWizard.diagram.caption')}</figcaption>
      {/* Text scales with the picture, so the picture never gets narrower than
          MIN_WIDTH (smallest label 10px); a phone scrolls it sideways instead. */}
      <div className="overflow-x-auto">
        <div data-testid={`slack-wizard-diagram-${step}`} role="img"
          aria-label={i18nT('pages.settings.slackWizard.diagram.alt')}
          className="relative w-full select-none pointer-events-none"
          style={{ aspectRatio: `${W} / ${H}`, containerType: 'inline-size', minWidth: MIN_WIDTH }}>
          <Frame>{body}</Frame>
        </div>
      </div>
    </figure>
  )
}
