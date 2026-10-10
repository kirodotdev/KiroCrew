/**
 * Registered UI locations on the Sessions (chat) page. One area of
 * `UI_LOCATIONS`; see `../descriptors.ts` for the two-step contract.
 */
import type { UiLocationArea } from '../types'

export const LOCATIONS = {
  'chat.sessions-sidebar-toggle': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label', key: 'pages.chatPage.show_sessions_sidebar' },
    aliasKeys: ['pages.chatPage.show_sessions'],
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [
        { kind: 'viewport', value: 'desktop' },
        { kind: 'condition', id: 'has_open_sessions' },
        { kind: 'condition', id: 'full_dashboard' },
      ],
    }],
  },
  'chat.mobile-sessions-toggle': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label' },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'header',
      requires: [{ kind: 'viewport', value: 'mobile' }],
    }],
  },
  'chat.older-sessions': {
    kind: 'disclosure',
    aliasKeys: ['pages.chatSidebar.older_sessions'],
    terms: {
      en: [
        'old chats', 'old sessions', 'old conversations', 'past chats', 'past conversations',
        'past sessions', 'previous chats', 'previous conversations', 'previous sessions',
        'closed chats', 'closed sessions', 'chat history', 'conversation history',
        'session history', 'history',
      ],
      'zh-CN': ['历史对话', '历史会话', '历史聊天', '会话历史', '对话历史', '聊天历史', '以前的聊天', '以前的会话', '以前的对话', '过去的对话', '旧会话', '旧聊天', '聊天记录', '历史记录'],
      hi: ['पुरानी चैट', 'पिछली बातचीत', 'पुराने सेशन', 'चैट इतिहास'],
      es: ['chats antiguos', 'conversaciones anteriores', 'historial de chats', 'historial'],
      fr: ['anciennes conversations', 'conversations précédentes', 'sessions précédentes', 'historique des discussions', 'historique'],
      bn: ['পুরনো চ্যাট', 'আগের কথোপকথন', 'চ্যাট ইতিহাস'],
      pt: ['conversas antigas', 'conversas anteriores', 'sessões anteriores', 'histórico de conversas', 'histórico'],
      ru: ['старые чаты', 'прошлые разговоры', 'предыдущие сессии', 'история чатов', 'история'],
      de: ['alte Chats', 'frühere Unterhaltungen', 'frühere Sitzungen', 'Chatverlauf', 'Verlauf'],
      ja: ['過去の会話', '以前のチャット', '古いセッション', 'チャット履歴', '履歴'],
      ko: ['이전 대화', '지난 대화', '채팅 기록', '대화 기록'],
      it: ['chat precedenti', 'conversazioni passate', 'sessioni precedenti', 'cronologia chat', 'cronologia'],
    },
    placements: [
      {
        surface: 'chat', parent: 'page.chat', entry: 'sidebar',
        // With no open sessions the sidebar is forced open and the toggle is not
        // drawn; while it is pinned open the toggle reads "Hide". So the toggle
        // is a step only while the sidebar is collapsed.
        requires: [
          { kind: 'viewport', value: 'desktop' },
          { kind: 'shown_by', location: 'chat.sessions-sidebar-toggle', when: 'sessions_sidebar_collapsed' },
        ],
      },
      {
        surface: 'chat', parent: 'page.chat', entry: 'sidebar',
        requires: [
          { kind: 'viewport', value: 'mobile' },
          { kind: 'shown_by', location: 'chat.mobile-sessions-toggle', when: 'sessions_drawer_closed' },
        ],
      },
    ],
  },
  // The New button in the sessions sidebar header (ChatSidebar). Its visible
  // text switches between "New" and "Creating…" and is hidden in a narrow
  // header, so the label is the static accessible name; "New chat" is its title.
  // Drawn in the sidebar, so it is reached exactly like Older Sessions.
  'chat.new-session': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    aliasKeys: ['pages.chatSidebar.new_chat'],
    terms: {
      en: [
        'new conversation', 'start a conversation', 'start a chat', 'start a new chat', 'begin a chat',
        'create a chat', 'new session', 'create a session', 'open a new chat',
      ],
      'zh-CN': ['新对话', '新聊天', '新建会话', '新建聊天', '开始对话', '开始聊天', '新开对话', '开一个新对话'],
      ja: ['新しい会話', 'チャットを始める'],
      ko: ['새 대화', '대화 시작'],
      es: ['nueva conversación', 'empezar un chat'],
      fr: ['nouvelle conversation', 'démarrer une discussion'],
      de: ['neue Unterhaltung', 'Chat starten'],
      pt: ['nova conversa', 'começar um chat'],
      it: ['nuova conversazione', 'iniziare una chat'],
      ru: ['новый разговор', 'начать чат'],
    },
    placements: [
      {
        surface: 'chat', parent: 'page.chat', entry: 'sidebar',
        requires: [
          { kind: 'viewport', value: 'desktop' },
          { kind: 'shown_by', location: 'chat.sessions-sidebar-toggle', when: 'sessions_sidebar_collapsed' },
        ],
      },
      {
        surface: 'chat', parent: 'page.chat', entry: 'sidebar',
        requires: [
          { kind: 'viewport', value: 'mobile' },
          { kind: 'shown_by', location: 'chat.mobile-sessions-toggle', when: 'sessions_drawer_closed' },
        ],
      },
    ],
  },
  // The chat pane's empty state (ChatPage), drawn only while no session is
  // open there: a different site, label and host from the sidebar button.
  'chat.start-new-chat': {
    kind: 'button',
    terms: {
      en: ['new conversation', 'start a conversation', 'begin a chat', 'create a chat'],
      'zh-CN': ['新对话', '新聊天', '开始对话', '新建对话'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'content',
      requires: [{ kind: 'condition', id: 'no_active_session' }],
    }],
  },
  // The model chip in the composer's shelf (ContextShelf ModelChip). Its text,
  // title and accessible name are the session's model and effort (runtime
  // data), so the index carries a description of what it is, never a label to
  // quote. Disabled while a response runs ("Stop the current response to
  // switch model"), hence the condition.
  'chat.model-picker': {
    kind: 'button',
    label: { from: 'description', attr: 'aria-label', key: 'uiLocations.description.chat_model_picker' },
    // What a guide's panel calls it: the chip shows the model's own name.
    aliasKeys: ['pages.kiroCrewAgentsPage.model'],
    terms: {
      en: [
        'change the model for this chat', 'change the model', 'switch the model', 'change model', 'switch model',
        'choose a model', 'pick a model', 'model picker', 'select a model', 'which model', 'reasoning effort',
      ],
      'zh-CN': ['换模型', '切换模型', '更换模型', '选择模型', '选模型', '模型选择', '推理强度'],
      ja: ['モデルを変更', 'モデルを切り替え'],
      ko: ['모델 변경', '모델 바꾸기'],
      es: ['cambiar el modelo', 'elegir modelo'],
      fr: ['changer de modèle', 'choisir un modèle'],
      de: ['Modell wechseln', 'Modell ändern'],
      pt: ['mudar o modelo', 'trocar de modelo'],
      it: ['cambiare modello', 'scegliere il modello'],
      ru: ['сменить модель', 'выбрать модель'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [
        { kind: 'condition', id: 'session_open' },
        // The shelf unmounts with a collapsed message box (ChatInput).
        { kind: 'shown_by', location: 'composer.expand', when: 'composer_collapsed' },
        { kind: 'condition', id: 'no_response_running' },
      ],
    }],
  },
  // The memory-mode chip (MemoryModeChip), above the composer or on the
  // orchestrator welcome screen: either way only while the open session has no
  // messages yet. Its text depends on the mode ("Choose memory mode", or the
  // way back from incognito/temporary), so it is a description, too.
  'chat.memory-mode': {
    kind: 'button',
    label: { from: 'description', key: 'uiLocations.description.chat_memory_mode' },
    terms: {
      en: [
        'memory mode', 'incognito', 'incognito mode', 'temporary chat', 'private chat',
        "don't remember this chat", 'turn off memory', 'chat without memory',
      ],
      'zh-CN': ['记忆', '记忆模式', '无痕模式', '隐身模式', '临时对话', '不保存记忆', '关闭记忆'],
      ja: ['メモリーモード', 'シークレットモード'],
      ko: ['메모리 모드', '시크릿 모드'],
      es: ['modo de memoria', 'modo incógnito'],
      fr: ['mode mémoire', 'mode incognito'],
      de: ['Gedächtnismodus', 'Inkognito-Modus'],
      pt: ['modo de memória', 'modo anônimo'],
      it: ['modalità memoria', 'modalità in incognito'],
      ru: ['режим памяти', 'режим инкогнито'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'content',
      requires: [{ kind: 'condition', id: 'empty_session' }],
    }],
  },
  // The session title in the chat header (SessionTitleControl): clicking it
  // opens the inline rename editor. Its text and accessible name are the
  // session's own name, so the index carries a description. The phone's top
  // bar draws the title inside the session menu's trigger instead (rename is a
  // row of that menu there), so this placement is desktop only.
  'chat.session-title': {
    kind: 'button',
    label: { from: 'description', attr: 'aria-label', key: 'uiLocations.description.chat_session_title' },
    terms: {
      en: [
        'rename this chat', 'rename a chat', 'rename the session', 'rename a session', 'change the chat name',
        'give this chat a different name', 'change the session title', 'edit the chat title', 'name this chat',
      ],
      'zh-CN': ['重命名会话', '重命名对话', '给对话改名', '对话改名', '修改会话名称', '修改对话标题', '改会话名字'],
      ja: ['チャットの名前を変更', 'セッション名を変更'],
      ko: ['채팅 이름 바꾸기', '세션 이름 변경'],
      es: ['renombrar el chat', 'cambiar el nombre de la sesión'],
      fr: ['renommer la discussion', 'renommer la session'],
      de: ['Chat umbenennen', 'Sitzung umbenennen'],
      pt: ['renomear o chat', 'renomear a sessão'],
      it: ['rinominare la chat', 'rinominare la sessione'],
      ru: ['переименовать чат', 'переименовать сеанс'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'header',
      requires: [{ kind: 'viewport', value: 'desktop' }, { kind: 'condition', id: 'session_open' }],
    }],
  },
  // The chat header's side-panel opener (ChatPage), drawn only while the panel
  // is closed; the panel's own header carries the close button. Desktop: the
  // phone's top bar replaces this header.
  'chat.side-panel-open': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['open the chat side panel', 'show the chat side panel', 'chat side panel', 'panel on the right of the chat'],
      'zh-CN': ['打开会话侧边面板', '会话侧边面板', '聊天右侧面板'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'header',
      requires: [
        { kind: 'viewport', value: 'desktop' },
        { kind: 'condition', id: 'session_open' },
        { kind: 'condition', id: 'full_dashboard' },
      ],
    }],
  },
  // The side panel's tab strip "+" (SidePanel): the menu of views a panel tab
  // can show. The same strip is drawn in the Crewmates page's panel, which is
  // not registered here.
  'chat.side-panel.add': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['add a side panel tab', 'open another panel view', 'side panel views'],
      'zh-CN': ['添加侧边面板标签页', '侧边面板视图'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'toolbar',
      requires: [{ kind: 'shown_by', location: 'chat.side-panel-open', when: 'side_panel_closed' }],
    }],
  },
  // The "+" menu's Browser row: opens the built-in browser view as a panel tab
  // (WebPreviewPanel), where a person watches and drives the agent's browser.
  // The empty panel's launcher grid offers the same view; not registered.
  'chat.side-panel.browser': {
    kind: 'menu-item',
    terms: {
      en: [
        'built-in browser', 'browser panel', 'browser view', 'open the browser', 'watch the agent browse',
        'see the web page the agent opened', 'open a web page in the dashboard',
      ],
      'zh-CN': ['内置浏览器', '浏览器面板', '浏览器视图', '打开浏览器', '看助手浏览网页', '仪表盘里打开网页'],
      ja: ['内蔵ブラウザ', 'ブラウザパネル'],
      ko: ['내장 브라우저', '브라우저 패널'],
      es: ['navegador integrado', 'panel del navegador'],
      fr: ['navigateur intégré', 'panneau du navigateur'],
      de: ['integrierter Browser', 'Browserbereich'],
      pt: ['navegador integrado', 'painel do navegador'],
      it: ['browser integrato', 'pannello del browser'],
      ru: ['встроенный браузер', 'панель браузера'],
    },
    placements: [{ surface: 'chat', parent: 'chat.side-panel.add', entry: 'menu' }],
  },
} as const satisfies UiLocationArea
