/**
 * Goldens for the `composer` area (the message box and its shelf): the exact
 * parent chain, entry, route, label key and requirements each location carries
 * in the committed index, and the marker sitting in the file it claims.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Req { kind: string; value?: string; location?: string; when?: string; id?: string }
interface Placement { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: Req[] }
interface Loc { id: string; kind: string; label_key: string; alias_keys?: string[]; placements: Placement[] }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as {
  locations: Loc[]
  labels: Record<string, Record<string, string>>
}
const byId = new Map(index.locations.map(l => [l.id, l]))
const src = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf-8')
const CHAT_INPUT = src('../components/ChatInput.tsx')
const ATTACH = src('../components/chat-input/attach.tsx')
const BUSY = src('../components/chat-input/busySend.tsx')
const SHELF = src('../components/chat-input/ContextShelf.tsx')
const COLLAPSE = src('../components/chat-input/collapse.tsx')
const VOICE = src('../components/chat-input/VoiceControls.tsx')
const APPROVAL = src('../components/ApprovalModePicker.tsx')
const AUTOMATION = src('../components/SessionAutomationPopover.tsx')
const AUTO_NUDGE = src('../components/AutoNudgePopover.tsx')

const OPEN: Req = { kind: 'condition', id: 'session_open' }
const DESKTOP: Req = { kind: 'viewport', value: 'desktop' }
const MOBILE: Req = { kind: 'viewport', value: 'mobile' }
const MOUSE: Req = { kind: 'condition', id: 'mouse_input' }
/** A collapsed message box unmounts every control here, so each names the way back. */
const EXPANDED: Req = { kind: 'shown_by', location: 'composer.expand', when: 'composer_collapsed' }

type Expect = {
  kind: string; labelKey: string; aliasKeys?: string[]; source: string
  en: string; zh: string
  parents: string[]; entry: string; requires: Req[]
  /** Requirements of further placements (same parents and entry), in order. */
  more?: Req[][]
}
const menuItem = (labelKey: string, en: string, zh: string, aliasKeys?: string[], extra: Req[] = []): Expect => ({
  kind: 'menu-item', labelKey, aliasKeys, source: ATTACH, en, zh,
  parents: ['page.chat', 'composer.add-menu'], entry: 'menu', requires: [DESKTOP, MOUSE, OPEN, EXPANDED, ...extra],
})

const EXPECTED: Record<string, Expect> = {
  'composer.expand': {
    kind: 'button', labelKey: 'components.chatInput.expand_composer', source: COLLAPSE,
    en: 'Show the message input', zh: '展开消息输入',
    parents: ['page.chat'], entry: 'toolbar', requires: [OPEN],
  },
  'composer.send': {
    kind: 'button', labelKey: 'components.chatInput.send', source: CHAT_INPUT, en: 'Send', zh: '发送',
    parents: ['page.chat'], entry: 'toolbar',
    requires: [OPEN, EXPANDED, { kind: 'condition', id: 'message_typed' }, { kind: 'condition', id: 'no_response_running' }],
  },
  'composer.stop': {
    kind: 'button', labelKey: 'components.chatInput.stop_generation', source: BUSY, en: 'Stop generation', zh: '停止生成',
    parents: ['page.chat'], entry: 'toolbar',
    requires: [OPEN, EXPANDED, { kind: 'condition', id: 'response_running' }, { kind: 'condition', id: 'message_box_empty' }],
  },
  'composer.add-menu': {
    kind: 'button', labelKey: 'components.chatInput.add_files_options', source: ATTACH,
    // Where files are attached on a desktop with a mouse.
    aliasKeys: ['components.chatInput.attach_files'],
    en: 'Add files & options', zh: '添加文件与选项',
    parents: ['page.chat'], entry: 'toolbar', requires: [DESKTOP, MOUSE, OPEN, EXPANDED],
  },
  'composer.attach-files': {
    kind: 'button', labelKey: 'components.chatInput.attach_files', source: ATTACH, en: 'Attach files', zh: '附加文件',
    parents: ['page.chat'], entry: 'toolbar', requires: [MOBILE, OPEN, EXPANDED],
    // A desktop-width touch device gets the direct picker instead of "+".
    more: [[DESKTOP, { kind: 'condition', id: 'touch_input' }, OPEN, EXPANDED]],
  },
  'composer.add-menu.upload': menuItem('components.chatInput.upload_file', 'Upload file', '上传文件'),
  'composer.add-menu.screenshot': menuItem(
    'components.chatInput.screenshot', 'Screenshot', '截图', undefined,
    [{ kind: 'condition', id: 'screen_capture_available' }],
  ),
  'composer.add-menu.sketch': menuItem('components.chatInput.sketch', 'Sketch', '草图'),
  'composer.add-menu.slash': menuItem('components.chatInput.command', 'Command', '命令', ['components.chatInput.slash_commands']),
  'composer.add-menu.reference-file': menuItem('components.chatInput.file', 'File', '文件', ['components.chatInput.reference_a_file']),
  'composer.add-menu.skill': menuItem('components.chatInput.skill', 'Skill', '技能', ['components.chatInput.use_a_skill']),
  'composer.context-usage': {
    kind: 'button', labelKey: 'components.chatInput.context_usage', source: SHELF, en: 'Context usage', zh: '上下文用量',
    parents: ['page.chat'], entry: 'toolbar', requires: [OPEN, EXPANDED, { kind: 'condition', id: 'context_usage_reported' }],
  },
  // The mic's name follows what a press does, so the index carries a description.
  'composer.voice': {
    kind: 'button', labelKey: 'uiLocations.description.composer_voice_input', source: VOICE,
    en: 'The microphone button in the message box. Press it to dictate instead of typing; on a touchscreen it switches between the keyboard and hold-to-talk.',
    zh: '消息框中的麦克风按钮，按下即可用语音输入代替打字；在触摸屏上它会在键盘和按住说话之间切换。',
    parents: ['page.chat'], entry: 'toolbar', requires: [OPEN, EXPANDED, { kind: 'condition', id: 'voice_input_supported' }],
  },
  // Its name follows what is set ("Set a goal", a running goal's cycle, the
  // monitor's status); the index carries the unset one.
  'composer.automation': {
    kind: 'button', labelKey: 'components.autoNudgePopover.set_a_goal', source: AUTOMATION,
    en: 'Set a goal',
    zh: '设定目标',
    parents: ['page.chat'], entry: 'toolbar', requires: [OPEN, EXPANDED],
  },
  // Inside its panel: Pause while a goal loop runs, Stop monitor while a
  // monitor has not finished.
  'composer.automation.pause': {
    kind: 'button', labelKey: 'components.autoNudgePopover.pause_loop', source: AUTO_NUDGE,
    en: 'Pause loop',
    zh: '暂停循环',
    parents: ['page.chat', 'composer.automation'], entry: 'content', requires: [OPEN, EXPANDED, { kind: 'condition', id: 'goal_loop_running' }],
  },
  'composer.automation.stop-monitor': {
    kind: 'button', labelKey: 'components.sessionAutomationPopover.stop_monitor', source: AUTOMATION,
    en: 'Stop monitor',
    zh: '停止监控',
    parents: ['page.chat', 'composer.automation'], entry: 'content', requires: [OPEN, EXPANDED, { kind: 'condition', id: 'monitor_running' }],
  },
  'composer.approval-mode': {
    kind: 'button', labelKey: 'uiLocations.description.composer_approval_mode', source: APPROVAL,
    aliasKeys: ['components.approvalModePicker.approval_mode'],
    en: "The approval-mode picker in the message box's control row. It sets whether tool calls ask first (Normal), run reads without asking (Reads), trust this session (Trust), or auto-approve everything (YOLO).",
    zh: '消息框控制栏中的审批模式选择器，用于设置工具调用是先询问（常规）、读取操作直接运行（读取）、信任本会话（信任），还是全部自动批准（YOLO）。',
    parents: ['page.chat'], entry: 'toolbar', requires: [OPEN, EXPANDED],
  },
}

describe('composer area locations', () => {
  it('indexes exactly the expected composer.* ids', () => {
    const ids = index.locations.map(l => l.id).filter(id => id.startsWith('composer.')).sort()
    expect(ids).toEqual(Object.keys(EXPECTED).sort())
  })

  for (const [id, e] of Object.entries(EXPECTED)) {
    it(`${id}: its placements with the exact path, entry and requirements`, () => {
      const loc = byId.get(id)
      expect(loc, id).toBeDefined()
      expect(loc!.kind).toBe(e.kind)
      expect(loc!.label_key).toBe(e.labelKey)
      expect(loc!.alias_keys).toEqual(e.aliasKeys)
      expect(loc!.placements).toEqual([e.requires, ...(e.more ?? [])].map(requires => ({
        surface_id: 'chat', route: '/chat', parent_ids: e.parents, entry_kind: e.entry, requires,
      })))
    })

    it(`${id}: labelled in English and Chinese`, () => {
      expect(index.labels.en[e.labelKey]).toBe(e.en)
      expect(index.labels['zh-CN'][e.labelKey]).toBe(e.zh)
    })

    it(`${id}: marked exactly once, in the file it claims`, () => {
      const marker = `uiLocation('${id}'`
      expect(e.source.split(marker).length - 1).toBe(1)
      for (const other of [CHAT_INPUT, ATTACH, BUSY, SHELF, COLLAPSE, VOICE, APPROVAL, AUTOMATION, AUTO_NUDGE].filter(s => s !== e.source)) {
        expect(other.includes(marker)).toBe(false)
      }
    })
  }
})
