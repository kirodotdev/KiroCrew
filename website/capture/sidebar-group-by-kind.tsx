/**
 * Capture page for the filter menu's "Group by kind" option (#25861): the REAL
 * `ChatSidebar` over Crew Mode, normal and crewmate sessions, in the folder tree.
 *
 * Query string: ?theme=dark|light&group=0|1
 */
import { MIN, at, localRow, mountLocalSidebar, prepareLocalSidebar } from './localSidebarFixture'

const params = new URLSearchParams(location.search)
prepareLocalSidebar(params)
localStorage.setItem('mc-sidebar-lane', 'tree')
localStorage.setItem('mc-sidebar-width', '360')
localStorage.setItem('mc-session-group-by-kind', params.get('group') === '1' ? '1' : '0')

const CONDUCTOR = 'kirocrew-conductor'

mountLocalSidebar([
  { ...localRow({ key: 'c-pin', title: 'Release checklist', last_ts: at(30 * MIN), last_message: 'Pinned on top.' }), pinned: true },
  localRow({ key: 'c-n1', title: 'Fix the login redirect', last_ts: at(1 * MIN), last_message: 'Tests pass.' }),
  { ...localRow({ key: 'c-crew', title: 'Ship the sidebar wave', agent: CONDUCTOR, last_ts: at(3 * MIN), last_message: 'Three workers running.' }), crew_mode_root: true },
  { ...localRow({ key: 'c-mate', title: 'Weekly report', agent: 'ava', last_ts: at(6 * MIN), last_message: 'Draft is ready.' }), agent_kind: 'member' },
  localRow({ key: 'c-n2', title: 'Explain the CDK stack', last_ts: at(9 * MIN), last_message: 'Four stacks, one account each.' }),
  { ...localRow({ key: 'c-crew2', title: 'Triage the PR queue', agent: CONDUCTOR, last_ts: at(14 * MIN), last_message: 'Two PRs need you.' }), crew_mode_root: true },
  localRow({ key: 'c-mate2', title: 'Follow-up from the report', parent: { slot: 'c-mate', key: 'c-mate' }, last_ts: at(20 * MIN), last_message: 'Opened by Ava.' }),
], ['kirocrew', CONDUCTOR])
