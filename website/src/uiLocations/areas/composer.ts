/**
 * Registered UI locations for the message box (composer) and its shelf:
 * ChatInput and `components/chat-input/*`. One area of `UI_LOCATIONS`; see
 * `../descriptors.ts` for the two-step contract. Created empty ahead of its
 * area batch, so filling it never edits the aggregator.
 */
import type { UiLocationArea, UiRequirement } from '../types'

const SESSION_OPEN: UiRequirement = { kind: 'condition', id: 'session_open' }
/**
 * A collapsed message box unmounts every control below and the shelf with it
 * (ChatInput), and the collapse is remembered across reloads, so the Expand
 * composer bar is a step whenever the box is collapsed. Menu items inherit it
 * from the "+" menu.
 */
const EXPANDED: UiRequirement = { kind: 'shown_by', location: 'composer.expand', when: 'composer_collapsed' }

export const LOCATIONS = {
  // The collapsed composer's way back (chat-input/collapse.tsx): a full-width
  // bar that stands where the message box was. Drawn only while collapsed.
  'composer.expand': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['show the message box', 'bring back the message box', 'where did the message box go', 'unhide the composer', 'message box is gone'],
      'zh-CN': ['展开输入框', '显示输入框', '输入框不见了', '恢复输入框'],
    },
    placements: [{ surface: 'chat', parent: 'page.chat', entry: 'toolbar', requires: [SESSION_OPEN] }],
  },
  // The idle send arrow (ChatInput). Its busy twins in busySend.tsx (steer-only
  // send, the split steer/queue button) are other states, so only this site is
  // marked: no response running and text typed (empty, it is disabled, or it
  // becomes Resume after a cut-off turn).
  'composer.send': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['send my message', 'send a message', 'submit my message', 'send the prompt'],
      'zh-CN': ['发送消息', '提交消息', '发出消息'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [
        SESSION_OPEN,
        EXPANDED,
        { kind: 'condition', id: 'message_typed' },
        { kind: 'condition', id: 'no_response_running' },
      ],
    }],
  },
  // The stop square (busySend.tsx), drawn while a response runs and nothing is
  // waiting to send: typed text OR an attached file turns this place into Queue
  // or Steer (`composerHasDraft`). A referenced session alone does not.
  'composer.stop': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['stop the answer', 'stop the response', 'stop responding', 'cancel the response', 'interrupt the agent', 'stop the agent', 'stop it talking', 'stop it from talking', 'stop the reply', 'stop generating', 'stop it while it is answering', 'stop the agent while it is answering'],
      'zh-CN': ['停止回答', '停止响应', '打断回答', '中止回答', '停止回复', '让它别说了'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [
        SESSION_OPEN,
        EXPANDED,
        { kind: 'condition', id: 'response_running' },
        { kind: 'condition', id: 'message_box_empty' },
      ],
    }],
  },
  // The "+" drop-up (attach.tsx). Drawn only with a mouse at desktop width
  // (`directFilePicker` is a phone or a touch device); a desktop-width touch
  // device gets composer.attach-files instead.
  'composer.add-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    // On a desktop with a mouse this "+" is where files are attached: the
    // touch path's "Attach files" button is not drawn there.
    aliasKeys: ['components.chatInput.attach_files'],
    terms: {
      en: ['attach a file', 'add an attachment', 'plus button', 'add to my message'],
      'zh-CN': ['附件', '添加附件', '加号按钮', '加号菜单'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [{ kind: 'viewport', value: 'desktop' }, { kind: 'condition', id: 'mouse_input' }, SESSION_OPEN, EXPANDED],
    }],
  },
  // The touch path's attach control (attach.tsx): a file-input label that
  // stands where "+" is on a phone or a touch device and opens the file picker
  // directly.
  'composer.attach-files': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['attach a file', 'add an attachment', 'attach an image', 'attach a photo', 'send a picture', 'send a photo'],
      'zh-CN': ['附件', '添加附件', '附加图片', '发图片', '发照片'],
    },
    placements: [
      {
        surface: 'chat', parent: 'page.chat', entry: 'toolbar',
        requires: [{ kind: 'viewport', value: 'mobile' }, SESSION_OPEN, EXPANDED],
      },
      {
        surface: 'chat', parent: 'page.chat', entry: 'toolbar',
        requires: [{ kind: 'viewport', value: 'desktop' }, { kind: 'condition', id: 'touch_input' }, SESSION_OPEN, EXPANDED],
      },
    ],
  },
  'composer.add-menu.upload': {
    kind: 'menu-item',
    terms: {
      en: ['upload an image to the chat', 'upload an image', 'upload a picture', 'upload a photo', 'send an image', 'attach an image', 'send a picture', 'send a photo'],
      'zh-CN': ['上传图片', '上传照片', '发送图片', '附加图片', '发图片', '发照片'],
    },
    placements: [{ surface: 'chat', parent: 'composer.add-menu', entry: 'menu' }],
  },
  // Drawn only where screen capture is supported (Electron snip or macOS).
  'composer.add-menu.screenshot': {
    kind: 'menu-item',
    terms: {
      en: ['take a screenshot', 'capture my screen', 'screen capture', 'screen grab'],
      'zh-CN': ['屏幕截图', '截屏', '截取屏幕'],
    },
    placements: [{
      surface: 'chat', parent: 'composer.add-menu', entry: 'menu',
      requires: [{ kind: 'condition', id: 'screen_capture_available' }],
    }],
  },
  // The "+" menu's Sketch row. The phone pencil and the tablet "More actions"
  // item are other hosts (ChatInput), not marked here.
  'composer.add-menu.sketch': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.chatInput.sketch' },
    terms: {
      en: ['draw a sketch', 'draw a picture', 'draw something', 'drawing', 'whiteboard'],
      'zh-CN': ['画草图', '画图', '手绘', '画板'],
    },
    placements: [{ surface: 'chat', parent: 'composer.add-menu', entry: 'menu' }],
  },
  // Visible text is "Command" (plus a description line); the title is
  // "Slash commands".
  'composer.add-menu.slash': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.chatInput.command' },
    aliasKeys: ['components.chatInput.slash_commands'],
    terms: {
      en: ['slash command', 'chat commands', 'quick commands', 'commands menu'],
      'zh-CN': ['斜杠', '快捷命令', '命令菜单', '聊天命令'],
    },
    placements: [{ surface: 'chat', parent: 'composer.add-menu', entry: 'menu' }],
  },
  'composer.add-menu.reference-file': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.chatInput.file' },
    aliasKeys: ['components.chatInput.reference_a_file'],
    terms: {
      en: ['mention a file', 'point the agent at a file', 'let the agent read a file', 'at mention a file'],
      'zh-CN': ['提及文件', '引用一个文件', '让代理读取文件'],
    },
    placements: [{ surface: 'chat', parent: 'composer.add-menu', entry: 'menu' }],
  },
  'composer.add-menu.skill': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.chatInput.skill' },
    aliasKeys: ['components.chatInput.use_a_skill'],
    terms: {
      en: ['apply a skill', 'run a skill', 'pick a skill', 'choose a skill'],
      'zh-CN': ['用技能', '选择技能', '应用技能', '调用技能'],
    },
    placements: [{ surface: 'chat', parent: 'composer.add-menu', entry: 'menu' }],
  },
  // The context-window bar in the shelf (ContextShelf). Drawn once the session
  // reports a context percentage.
  'composer.context-usage': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['how much context is left', 'context left', 'remaining context', 'context window', 'token usage', 'tokens used'],
      'zh-CN': ['上下文还剩多少', '剩余上下文', '上下文窗口', '令牌用量', '上下文还剩'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [SESSION_OPEN, EXPANDED, { kind: 'condition', id: 'context_usage_reported' }],
    }],
  },
  // The mic (chat-input/VoiceControls MicButton), drawn only where the browser
  // can record. Its name follows what a press does right now (Voice input,
  // Stop recording, Transcribing, or on a touchscreen Switch to voice /
  // Switch to keyboard), so the index carries a description.
  'composer.voice': {
    kind: 'button',
    label: { from: 'description', attr: 'aria-label', key: 'uiLocations.description.composer_voice_input' },
    terms: {
      en: [
        'voice input', 'dictate a message', 'dictation', 'talk instead of typing', 'speak my message',
        'microphone', 'use my voice', 'speech to text', 'voice typing',
      ],
      'zh-CN': ['语音输入', '用语音说', '麦克风', '说话输入', '语音转文字', '不想打字', '语音按钮'],
      ja: ['音声入力', 'マイク'],
      ko: ['음성 입력', '마이크'],
      es: ['entrada de voz', 'dictar un mensaje'],
      fr: ['saisie vocale', 'dicter un message'],
      de: ['Spracheingabe', 'Nachricht diktieren'],
      pt: ['entrada de voz', 'ditar uma mensagem'],
      it: ['input vocale', 'dettare un messaggio'],
      ru: ['голосовой ввод', 'надиктовать сообщение'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [SESSION_OPEN, EXPANDED, { kind: 'condition', id: 'voice_input_supported' }],
    }],
  },
  // The approval-mode picker in the composer's control row (ApprovalModePicker):
  // a drop-up of Normal / Reads / Trust / YOLO. Its text is the current mode
  // (icon only on a phone) and its accessible name interpolates it, so the
  // index carries a description; "Approval mode" (its tooltip) is an alias.
  // The goal / monitor button beside the message box (SessionAutomationPopover):
  // a target icon until something is armed, then a radar with its count. Its
  // name is the state ("Set a goal", "Monitor: active"), so the index carries a
  // description; a guide's panel calls it by its unarmed name.
  // The goal and monitor button: "Set a goal" until one is set, then a
  // running goal's cycle or the monitor's status. Its panel holds Pause (a
  // goal loop) or Stop monitor (a structured monitor).
  'composer.automation': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label', key: 'components.autoNudgePopover.set_a_goal' },
    terms: {
      en: [
        'auto nudge', 'keep working until done', 'monitor this session', 'session monitor',
        'goal for this chat',
      ],
      'zh-CN': ['自动 nudge', '自动推进', '设置目标', '会话监控', '监控这个会话'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [SESSION_OPEN, EXPANDED],
    }],
  },
  // The goal panel's Pause (a goal loop running in this chat): stops the
  // nudges in place; the loop can be resumed from the same panel.
  'composer.automation.pause': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['stop auto nudge', 'stop the auto nudge', 'turn off auto nudge', 'pause auto nudge', 'stop the goal', 'pause the goal loop', 'stop nudging'],
      'zh-CN': ['关闭自动 nudge', '关闭会话的自动 nudge', '停止自动推进', '暂停自动推进', '停止目标', '暂停目标循环'],
    },
    placements: [{
      surface: 'chat', parent: 'composer.automation', entry: 'content',
      requires: [SESSION_OPEN, EXPANDED, { kind: 'condition', id: 'goal_loop_running' }],
    }],
  },
  // The monitor panel's Stop monitor (a structured monitor that has not
  // finished): asks to confirm, then ends the monitor for good.
  'composer.automation.stop-monitor': {
    kind: 'button',
    terms: {
      en: ['stop the monitor', 'stop monitoring', 'turn off the monitor', 'end the session monitor'],
      'zh-CN': ['关闭监控', '结束会话监控', '停掉监控'],
    },
    placements: [{
      surface: 'chat', parent: 'composer.automation', entry: 'content',
      requires: [SESSION_OPEN, EXPANDED, { kind: 'condition', id: 'monitor_running' }],
    }],
  },
  'composer.approval-mode': {
    kind: 'button',
    label: { from: 'description', key: 'uiLocations.description.composer_approval_mode' },
    aliasKeys: ['components.approvalModePicker.approval_mode'],
    terms: {
      en: [
        'auto approve', 'auto-approve tools', 'stop asking for approval', 'stop asking me to approve',
        'yolo mode', 'turn on yolo', 'trust mode', 'reads mode', 'normal mode', 'tool permissions',
        'approve everything automatically',
      ],
      'zh-CN': ['自动批准', '自动审批', '不要每次都问我', 'YOLO 模式', '信任模式', '工具权限', '批准模式'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [SESSION_OPEN, EXPANDED],
    }],
  },
} as const satisfies UiLocationArea
