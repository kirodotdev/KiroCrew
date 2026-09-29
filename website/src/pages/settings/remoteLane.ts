/**
 * What a crew card SAYS, decided away from the JSX that draws it.
 *
 * The Crews-on-cloud card carries four things: the crew's face, its name, ONE lane
 * chip, and one plain-words line about where that crew is right now. Everything
 * technical — the SSH host and port, the ECS task target, the instance id, the
 * transport acronym — moved behind the kebab's Details, because a reader scanning
 * this list is answering "which crew, and can I use it", and an identifier answers
 * neither. Details is where an identifier IS the question, and there it is a lookup
 * table rather than prose.
 *
 * These are pure functions over an `InstanceView` on purpose. The words are the part
 * that has been wrong most often — a fargate row described as having a dashboard, a
 * cloud row described as "added by you" and offered a Remove that leaves an instance
 * billing — and a pure function is the part a test can pin without rendering a panel
 * that owns nineteen mutations.
 */
import type { InstanceView } from '../../api/client'
import { BUILTIN_PROVISIONER_ID, usesSsmTransport } from '../../utils/remoteCrew'
import { fmtTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/**
 * The four lanes a crew can run in, as a user names them.
 *
 * NOT the transport. `ssm` is how the dashboard reaches an EC2 machine and also how
 * it reaches a Fargate task, so the transport cannot tell those two apart, and the
 * old card wore both acronyms (`EC2` `SSM`) to compensate. The lane is the thing the
 * user chose when they deployed, which is the thing they recognise.
 */
export type RemoteLane = 'ssh' | 'ec2' | 'fargate'

/**
 * Whether a row is a MATE rather than a CREW, which is what decides the tab it lists in.
 *
 * A CREW is a gateway: a machine running `kirocrew gateway`, serving a whole roster,
 * with a dashboard you switch to from the top header. A MATE is one agent: a Fargate
 * task holding a single agent spec (`agent.json` in the crew bundle), serving a chat
 * API, with no dashboard and no roster.
 *
 * Read off the transport because that IS the distinction today: `fargate` is the only
 * connection method that reaches a single-agent task, and every other row -- an SSH
 * machine, an EC2 instance over Session Manager -- is a gateway. When a second mate lane
 * lands, this is the one function that learns about it, rather than every list and card
 * asking the question its own way.
 */
export function isMate(inst: InstanceView): boolean {
  return inst.connection_method === 'fargate'
}

/** The lane a registered crew belongs to. */
export function laneOf(inst: InstanceView): RemoteLane {
  if (inst.connection_method === 'fargate') return 'fargate'
  // Every remaining SSM row is a machine: the EC2 launcher's own, or one an operator
  // registered by instance id. Both are "an instance in your account", which is what
  // the EC2 chip claims — and the chip is deliberately not conditioned on a launch
  // job, because a row we cannot correlate is still an instance.
  if (usesSsmTransport(inst)) return 'ec2'
  // An EC2-STAMPED row reached over plain SSH is still an EC2 instance. The lane is
  // where the crew RUNS, not how the dashboard dials it, so the stamp decides here —
  // which is also what the old card said with its EC2 badge on exactly these rows.
  // Dropping that would call a live, billing instance an SSH machine.
  if (inst.provisioner_id === BUILTIN_PROVISIONER_ID) return 'ec2'
  return 'ssh'
}

/**
 * The chip's text.
 *
 * Through the catalog even though all four are product names that read the same in
 * every locale — the repo's own `type_ssh` / `type_fargate` keys are exactly that, and
 * the alternative is four bare literals in a user-visible position, which the strict
 * i18n gate reads (correctly) as untranslated copy. A key also leaves room for a
 * locale that transliterates one of them.
 */
export function laneLabel(lane: RemoteLane): string {
  return lane === 'ssh'
    ? i18nT('pages.settings.remoteCrewPanel.lane_label_ssh')
    : lane === 'ec2'
      ? i18nT('pages.settings.remoteCrewPanel.lane_label_ec2')
      : i18nT('pages.settings.remoteCrewPanel.lane_label_fargate')
}

/**
 * Whether this lane's status is a READING of something outside the dashboard.
 *
 * It decides two things on the card: whether the status line carries the moment it
 * was read, and whether it offers "Check again". A cloud lane's answer comes from
 * AWS through a poll, so it has an age and re-reading it can change it. An SSH
 * tunnel is this process's own socket — it has no age worth printing, and a refresh
 * button beside it would promise news that only a reconnect can produce.
 *
 * Asked of the TRANSPORT rather than of the lane, because the two differ for one real
 * row: an EC2-stamped crew reached over plain SSH runs in the EC2 lane, and its status
 * is still a local socket. Keying this on the lane would print an age for it that no
 * remote read produced.
 */
export function laneHasLiveReading(inst: InstanceView): boolean {
  return usesSsmTransport(inst)
}

/** How the status dot paints. Theme tokens only. */
export function statusDotClass(inst: InstanceView): string {
  switch (inst.status.state) {
    case 'connected':
      return 'bg-ok'
    case 'connecting':
      return 'bg-warn'
    case 'error':
      return 'bg-danger'
    default:
      return 'bg-muted'
  }
}

/**
 * The status line's words: what this crew is doing, in language that does not
 * require knowing what a tunnel is.
 *
 * A connected Fargate crew says "Running" rather than "Connected" because the two
 * answer different questions and only one of them is true of it: the task is running
 * in AWS, and what the user gets from it is a chat, never a dashboard to connect to.
 */
export function statusWords(inst: InstanceView): string {
  const lane = laneOf(inst)
  switch (inst.status.state) {
    case 'connected':
      return lane === 'fargate'
        ? i18nT('pages.settings.remoteCrewPanel.plain_running')
        : i18nT('pages.settings.remoteCrewPanel.plain_connected')
    case 'connecting':
      return i18nT('pages.settings.remoteCrewPanel.plain_starting')
    case 'error':
      return i18nT('pages.settings.remoteCrewPanel.plain_not_reachable')
    case 'stopped':
      return i18nT('pages.settings.remoteCrewPanel.plain_stopped')
    default:
      return i18nT('pages.settings.remoteCrewPanel.plain_not_connected')
  }
}

/**
 * The one-line caption under the status: what this crew IS, and the single
 * consequence a reader acts on.
 *
 * One line, and short. The sentences it replaces ran to three lines each and said
 * the same two things every card said — that Remove only unregisters, and that AWS
 * keeps billing — which is exactly the content that belongs in Details, once, beside
 * the identifiers it is about. What stays here is the part that differs per card.
 *
 * `correlatedCloud` is true when a launch job in this gateway's store names this
 * machine, so the cloud lifecycle (Stop / Start / Delete) is reachable for it.
 */
export function captionWords(inst: InstanceView, correlatedCloud: boolean): string {
  if (inst.connection_method === 'fargate') {
    return i18nT('pages.settings.remoteCrewPanel.cap_fargate_chat_only')
  }
  if (correlatedCloud) return i18nT('pages.settings.remoteCrewPanel.cap_launched_by_kiro_crew')
  if (laneOf(inst) === 'ec2') {
    // An instance in the user's account that this gateway did not launch, or launched
    // before it kept records. Either way Remove unregisters and bills on, and that is
    // the one consequence worth a card line; Details carries the rest.
    return i18nT('pages.settings.remoteCrewPanel.cap_may_still_bill')
  }
  return i18nT('pages.settings.remoteCrewPanel.cap_added_by_you')
}

/** One Details row: a label and the verbatim value under it. */
export interface CrewDetailRow {
  label: string
  value: string
}

/**
 * The identifiers the card no longer shows, as a lookup table.
 *
 * Verbatim and UNTRUNCATED, which is the whole point of moving them here: the card
 * had to shorten an ECS target to fit one line, and two tasks in a cluster differ
 * only in the tail that shortening cut off. Nothing here is abbreviated, because a
 * reader who opened Details is comparing a value to one they hold.
 */
export function detailRows(inst: InstanceView): CrewDetailRow[] {
  // The TRANSPORT, not the lane: what to call the connection and which identifier
  // addresses the machine are both questions about how the dashboard reaches it, and an
  // EC2-stamped row over plain SSH has an SSH host and no instance id at all. Labelling
  // that row "EC2 over Session Manager" beside an empty Instance value would describe a
  // connection it does not have.
  const method = inst.connection_method
  const rows: CrewDetailRow[] = [
    {
      label: i18nT('pages.settings.remoteCrewPanel.detail_connection'),
      value:
        method === 'fargate'
          ? i18nT('pages.settings.remoteCrewPanel.detail_conn_fargate')
          : method === 'ssm'
            ? i18nT('pages.settings.remoteCrewPanel.detail_conn_ssm')
            : i18nT('pages.settings.remoteCrewPanel.detail_conn_ssh'),
    },
  ]
  if (method === 'fargate') {
    rows.push({ label: i18nT('pages.settings.remoteCrewPanel.detail_task'), value: inst.ssm_target })
  } else if (method === 'ssm') {
    rows.push({
      label: i18nT('pages.settings.remoteCrewPanel.detail_instance'),
      value: inst.ssm_target,
    })
  } else if (inst.via_instance_id) {
    // A chained row's host is a RECORD, not a target: this gateway resolves the
    // PARENT's host for any row carrying `via_instance_id` and never dials this one.
    // Labelled as reported so an untrusted string cannot borrow the authority of a
    // verified address, and the port is dropped — it is the crew's own gateway port,
    // which is not what this dashboard forwards to either.
    rows.push({
      label: i18nT('pages.settings.remoteCrewPanel.detail_reported_host'),
      value: inst.ssh_host,
    })
  } else {
    rows.push({
      label: i18nT('pages.settings.remoteCrewPanel.detail_host'),
      value: `${inst.ssh_host} ${i18nT('pages.settings.instancesPanel.port_2')} ${inst.remote_port}`,
    })
  }
  if (inst.aws_region) {
    rows.push({
      label: i18nT('pages.settings.remoteCrewPanel.detail_region'),
      value: inst.aws_region,
    })
  }
  if (inst.aws_profile) {
    rows.push({
      label: i18nT('pages.settings.instancesPanel.aws_profile'),
      value: inst.aws_profile,
    })
  }
  return rows
}

/**
 * The long-form note Details closes with: what this crew is, and what Remove does
 * and does not do.
 *
 * These are the sentences the cards used to carry. They are kept word for word
 * rather than rewritten, because each one was arrived at by correcting a specific
 * wrong reading — that a fargate Remove stops the task, that an EC2 stamp might not
 * mean AWS resources exist — and shortening them here would undo those corrections.
 * Moving them is the change; rewording them is not.
 *
 * Returns the TEXT, not a catalog key. A key returned from here and passed to
 * `i18nT` at the call site is a key the reference gate cannot resolve — it checks
 * call sites, and an assembled or forwarded key reads as dynamic — so the literal
 * calls live here, where the gate can see all four.
 */
export function detailNote(inst: InstanceView, correlatedCloud: boolean): string {
  if (correlatedCloud) return i18nT('pages.settings.remoteCrewPanel.launched_by_kiro_crew')
  if (inst.connection_method === 'fargate') {
    return i18nT('pages.settings.remoteCrewPanel.fargate_task_note')
  }
  // An EC2-STAMPED row was launched by the EC2 launcher, and the note says so plainly:
  // its instance may still be running and billing, and Remove only unregisters it. A row
  // with no stamp gets the hedge instead, which is the distinction the cards drew before
  // these sentences moved here — the caption must not claim AWS resources exist when
  // nothing proves they do, nor hedge when the stamp says they were created.
  if (inst.provisioner_id === BUILTIN_PROVISIONER_ID) {
    return i18nT('pages.settings.remoteCrewPanel.stamped_ec2_note')
  }
  if (laneOf(inst) === 'ec2') return i18nT('pages.settings.remoteCrewPanel.unverified_cloud_note')
  return i18nT('pages.settings.remoteCrewPanel.doesnt_manage')
}

/**
 * The clock time a reading was taken at, in the locale the DASHBOARD is set to.
 *
 * Through `fmtTime` rather than `toLocaleTimeString`, which reads the HOST locale: a
 * German UI on an English machine would print `3:04 PM` beside German words, and
 * `localeFormatting.test.ts` gates exactly that on added lines.
 */
export function readClock(epochMs: number): string {
  if (!epochMs) return ''
  return fmtTime(new Date(epochMs))
}
