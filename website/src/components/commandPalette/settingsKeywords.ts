/**
 * Settings search words: the words people use for a setting that are NOT its
 * label. Every setting has an entry (settingsKeywords.test.ts fails on a
 * setting without one, and on an unknown id).
 *
 * Two searches read this one list:
 *  - the command palette's Settings provider merges it into its corpus, so
 *    "dark mode" finds the "Mode" setting on the Display tab;
 *  - the guide's find_ui tool gets it as each setting's English search terms
 *    (website/scripts/lib/ui-index.mjs). There a single word ("apps",
 *    "time") is left out unless GUIDE_ONE_WORD_TERMS below keeps it, and a
 *    word that only repeats this setting's label or is another location's
 *    name or search term is always left out. So is a word that sits inside
 *    another location's short label ("bot token" on WeCom, inside "Discord
 *    bot token"), unless GUIDE_ONE_WORD_TERMS names it: find_ui then shows
 *    the tie. One list can still carry the palette's literal spellings and short
 *    fragments. Give every setting at least one phrase of two or more words
 *    that find_ui keeps.
 * Other languages' words for a setting live in SEARCH_TERMS
 * (src/uiLocations/descriptors.ts), which refuses English ones for a setting.
 *
 * Write what a person would type or ask ("turn off auto update", "scroll to
 * bottom"), not a restatement of the label. At most 16 words per setting once
 * spellings that differ only in punctuation are merged.
 */
export const SETTINGS_KEYWORDS: Record<string, string[]> = {
  // About. fuzzyMatch is literal, so the spaced and hyphenated spellings are
  // both listed; "auto-update on restart" is the name both switches shipped under.
  'about.install-app-updates-automatically': ['auto update', 'auto-update', 'automatic updates', 'auto update on restart', 'auto-update on restart', 'auto download', 'app updates', 'turn off auto update', 'stop automatic updates'],
  'about.release-notes': ['release notes', 'releases', 'changelog', 'change log', "what's new", 'whats new', 'what changed'],
  'about.update-channel': ['stable', 'insider', 'nightly', 'release channel', 'beta', 'preview builds', 'switch to nightly'],
  'about.update-the-gateway-automatically': ['auto update', 'auto-update', 'automatic updates', 'gateway updates', 'auto update on restart', 'auto-update on restart', 'install updates', 'update check', 'turn off auto update', 'stop automatic updates', 'disable updates', 'update installed restart'],
  'about.version': ['version', 'about', 'build', 'what version', 'licence', 'license', 'check for updates', 'which version am i on'],

  // Agent
  'agent.engine': ['agent engine', 'kas', 'kiro agent engine', 'engine version', 'switch engine'],
  'agent.kiro-memory': ['kiro memories', 'use my kiro memory', 'kiro cli memory', 'memories from kiro'],

  // Browser
  'browser.browser-isolation': ['separate browser profile', 'isolated browser', 'sandboxed browser', 'browser profile', 'browser cookies'],
  'browser.max-frame-rate': ['fps', 'frames per second', 'live view smoothness', 'browser fps'],
  'browser.quality': ['browser image quality', 'live view quality', 'jpeg quality', 'browser stream quality'],
  'browser.run-with-no-window': ['headless', 'headless browser', 'hide browser window', 'background browser'],
  'browser.show-the-live-view-in-chat': ['browser preview', 'watch the browser', 'live browser view', 'see what the agent browses'],
  'browser.which-browser': ['chrome', 'chromium', 'edge', 'firefox', 'brave', 'pick a browser', 'browser choice'],

  // Channels: Slack
  'channels.agent-sessions-slack': ['slack agent sessions', 'sessions started from slack', 'slack chats'],
  'channels.allowed-enterprise-orgs-slack': ['enterprise grid', 'slack org allowlist', 'slack workspace ids', 'slack team id'],
  'channels.always-use-conductor-mode-slack': ['conductor mode in slack', 'turn off conductor mode in slack', 'slack conductor', 'slack threads as tasks'],
  'channels.code-channels-slack': ['slack channel per task', 'coding channels', 'slack code channel', 'diff tab in slack'],
  'channels.connect-new-sessions-to-slack-automatically-slack': ['mirror chats to slack', 'post new chats to slack', 'slack dm for every chat', 'auto connect slack'],
  'channels.file-sessions-in-a-different-folder-slack': ['slack chats folder', 'slack sessions folder', 'put slack sessions in a folder', 'group slack chats'],
  'channels.folder-name-slack': ['slack folder name', 'name of the slack folder', 'rename slack folder'],
  'channels.let-code-channels-start-helper-agents-slack': ['helper agents in slack', 'code channel helpers', 'subagents in code channels'],
  'channels.owner-slack-member-id-slack': ['slack member id', 'my slack id', 'slack user id', 'kirocrew_owner_id', 'slack owner'],
  'channels.phase-reactions-slack': ['slack emoji reactions', 'progress reactions', 'eyes emoji', 'status emoji in slack'],
  'channels.share-slack-access-slack': ['share slack bot', 'let others use my slack bot', 'slack teammates', 'shared slack'],
  'channels.show-slack-conversations-in-the-session-list-slack': ['slack chats in sidebar', 'show slack sessions in my list', 'hide slack sessions from the sidebar', 'slack threads in sessions list'],
  'channels.show-thinking-slack': ['slack reasoning', 'show reasoning in slack', 'thinking in slack', 'hide thinking in slack'],
  'channels.slack-app-token-slack': ['xapp token', 'socket mode token', 'slack app-level token'],
  'channels.slack-bot-token-slack': ['xoxb token', 'slack oauth token', 'slack bot oauth token'],
  'channels.slack-tools-for-your-agents-slack': ['read slack', 'search slack', 'slack search for agents', 'slack mcp', 'connect my slack account'],
  'channels.slash-command-slack': ['slack slash command', 'slack trigger word', 'slash command name'],
  'channels.tracked-channels-slack': ['slack channels the agent reads', 'which slack channels', 'watch slack channels', 'add a slack channel'],

  // Channels: Discord
  'channels.allowed-user-ids-discord': ['discord allowlist', 'who can message on discord', 'discord users'],
  'channels.discord-bot-token': ['discord token', 'discord bot', 'connect discord', 'set up discord'],
  'channels.file-sessions-in-a-folder-discord': ['discord chats folder', 'put discord sessions in a folder', 'group discord chats'],
  'channels.folder-name-discord': ['discord folder name', 'name of the discord folder', 'rename discord folder'],
  'channels.soft-context-threshold-discord': ['discord compact suggestion', 'discord context warning', 'suggest compact on discord'],

  // Channels: Feishu
  'channels.allowed-user-ids-feishu': ['feishu allowlist', 'lark allowlist', 'who can message on feishu', 'feishu users'],
  'channels.feishu-app-id': ['connect feishu', 'set up feishu', 'lark app id', 'feishu bot', 'connect lark'],
  'channels.feishu-app-secret': ['feishu secret', 'lark app secret', 'feishu credentials'],
  'channels.file-sessions-in-a-folder-feishu': ['feishu chats folder', 'put feishu sessions in a folder', 'group feishu chats'],
  'channels.folder-name-feishu': ['feishu folder name', 'name of the feishu folder', 'rename feishu folder'],
  'channels.soft-context-threshold-feishu': ['feishu compact suggestion', 'feishu context warning', 'suggest compact on feishu'],

  // Channels: iMessage
  'channels.allowed-handles-imessage': ['imessage allowlist', 'who can text the agent', 'imessage contacts'],
  'channels.enable-imessage-channel-imessage': ['turn on imessage', 'connect imessage', 'set up imessage', 'imessage bot'],
  'channels.file-sessions-in-a-folder-imessage': ['imessage chats folder', 'put imessage sessions in a folder', 'group imessage chats'],
  'channels.folder-name-imessage': ['imessage folder name', 'name of the imessage folder', 'rename imessage folder'],
  'channels.messages-database-path-imessage': ['chat.db', 'imessage database', 'messages db path'],
  'channels.send-replies-over-imessage': ['sms or imessage', 'reply by sms', 'imessage or sms'],

  // Channels: Teams
  'channels.allowed-users-email-or-aad-object-id-teams': ['teams allowlist', 'who can message on teams', 'azure ad object id', 'microsoft teams users'],
  'channels.app-client-id-teams': ['teams app id', 'azure app id', 'microsoft app id', 'teams bot client id'],
  'channels.app-password-client-secret-teams': ['teams client secret', 'azure client secret', 'microsoft app password', 'teams bot password'],
  'channels.enable-teams-channel-teams': ['turn on teams', 'connect microsoft teams', 'set up teams', 'teams bot'],
  'channels.file-sessions-in-a-folder-teams': ['teams chats folder', 'put teams sessions in a folder', 'group teams chats'],
  'channels.folder-name-teams': ['teams folder name', 'name of the teams folder', 'rename teams folder'],
  'channels.hard-context-threshold-teams': ['teams auto compact', 'teams context limit', 'force compaction on teams'],
  'channels.soft-context-threshold-teams': ['teams compact suggestion', 'teams context warning', 'suggest compact on teams'],
  'channels.tenant-id-teams': ['azure tenant', 'teams tenant', 'single tenant bot', 'azure directory id'],

  // Channels: Telegram
  'channels.allowed-user-ids-telegram': ['telegram allowlist', 'who can message on telegram', 'telegram users'],
  'channels.file-sessions-in-a-folder-telegram': ['telegram chats folder', 'put telegram sessions in a folder', 'group telegram chats'],
  'channels.folder-name-telegram': ['telegram folder name', 'name of the telegram folder', 'rename telegram folder'],
  'channels.soft-context-threshold-telegram': ['telegram compact suggestion', 'telegram context warning', 'suggest compact on telegram'],
  'channels.telegram-bot-token': ['telegram token', 'botfather token', 'connect telegram', 'set up telegram'],

  // Channels: Webex
  'channels.allowed-emails-webex': ['webex allowlist', 'who can message on webex', 'webex allowed people'],
  'channels.allowed-space-ids-webex': ['webex spaces', 'webex rooms', 'webex space allowlist'],
  'channels.answer-in-group-spaces-webex': ['webex group chats', 'reply in webex spaces', 'webex groups'],
  'channels.enable-webex-channel-webex': ['turn on webex', 'connect webex', 'set up webex', 'webex bot'],
  'channels.file-sessions-in-a-folder-webex': ['webex chats folder', 'put webex sessions in a folder', 'group webex chats'],
  'channels.folder-name-webex': ['webex folder name', 'name of the webex folder', 'rename webex folder'],
  'channels.hard-context-threshold-webex': ['webex auto compact', 'webex context limit', 'force compaction on webex'],
  'channels.reply-in-thread-webex': ['webex threads', 'webex thread replies'],
  'channels.soft-context-threshold-webex': ['webex compact suggestion', 'webex context warning', 'suggest compact on webex'],
  'channels.webex-bot-token-webex': ['webex token', 'webex bot access token', 'webex access token'],

  // Channels: WeChat
  'channels.allowed-user-ids-wechat': ['wechat allowlist', 'who can message on wechat', 'wechat users'],
  'channels.enable-the-wechat-channel-wechat': ['turn on wechat', 'connect wechat', 'set up wechat', 'weixin'],
  'channels.file-sessions-in-a-folder-wechat': ['wechat chats folder', 'put wechat sessions in a folder', 'group wechat chats'],
  'channels.folder-name-wechat': ['wechat folder name', 'name of the wechat folder', 'rename wechat folder'],
  'channels.who-can-message-the-agent-wechat': ['wechat access', 'who can use the wechat bot', 'wechat allowlist mode'],

  // Channels: WeCom. The bot token WeCom calls a secret.
  'channels.allowed-user-ids-wecom': ['wecom allowlist', 'who can message on wecom', 'wecom users'],
  'channels.file-sessions-in-a-folder-wecom': ['wecom chats folder', 'put wecom sessions in a folder', 'group wecom chats'],
  'channels.folder-name-wecom': ['wecom folder name', 'name of the wecom folder', 'rename wecom folder'],
  'channels.soft-context-threshold-wecom': ['wecom compact suggestion', 'wecom context warning', 'suggest compact on wecom'],
  'channels.wecom-bot-id': ['connect wecom', 'set up wecom', 'wecom ai bot', 'enterprise wechat bot'],
  'channels.wecom-bot-secret': ['bot token', 'wecom secret', 'wecom bot token'],

  // Channels: WhatsApp
  'channels.allowed-numbers-whatsapp': ['whatsapp allowlist', 'phone numbers', 'who can message on whatsapp'],
  'channels.enable-the-whatsapp-channel-whatsapp': ['turn on whatsapp', 'connect whatsapp', 'set up whatsapp', 'whatsapp bot'],
  'channels.file-sessions-in-a-folder-whatsapp': ['whatsapp chats folder', 'put whatsapp sessions in a folder', 'group whatsapp chats'],
  'channels.folder-name-whatsapp': ['whatsapp folder name', 'name of the whatsapp folder', 'rename whatsapp folder'],
  'channels.how-the-agent-joins-in-whatsapp': ['whatsapp group chats', 'agent in whatsapp groups', 'whatsapp mentions'],
  'channels.seconds-between-unprompted-replies-whatsapp': ['whatsapp rate limit', 'whatsapp reply cooldown', 'how often the agent speaks on whatsapp'],
  'channels.when-the-agent-may-speak-whatsapp': ['whatsapp unprompted replies', 'agent speaks up on whatsapp', 'whatsapp reply mode'],
  'channels.who-can-message-the-agent-whatsapp': ['whatsapp access', 'who can use the whatsapp bot', 'whatsapp allowlist mode'],

  // Chat
  'chat.apply-folder-suggestions-automatically': ['auto file sessions', 'auto sort chats into folders', 'move chats into folders automatically'],
  'chat.auto-compact-threshold': ['context window', 'compaction', 'memory', 'conversation length', 'auto compact', 'compact at percent', 'context is full', 'summarize old messages', 'compacts too early'],
  'chat.auto-open-board-in-side-panel': ['open the board automatically', 'board side panel', 'show board on start'],
  'chat.auto-open-git-in-side-panel': ['open the git panel automatically', 'git side panel', 'show git changes'],
  'chat.auto-title-refresh-interval-turns': ['session title refresh', 'rename chats automatically', 'auto title', 'chat titles'],
  'chat.background-effort': ['background reasoning effort', 'effort for background tasks', 'helper model effort'],
  'chat.background-model': ['model for titles and summaries', 'helper model', 'cheap model for background work', 'background tasks model'],
  'chat.compact-empty-folders': ['collapse empty folders', 'shrink empty folders', 'empty folder rows'],
  'chat.compaction-time-limit-seconds': ['compaction timeout', 'compact timeout', 'how long compaction may take'],
  'chat.completion-event-characters': ['subagent result length', 'completion message length', 'worker report length'],
  'chat.completion-event-truncation': ['cut off subagent results', 'truncate completion events', 'worker report truncation'],
  'chat.confirm-before-closing-session': ['ask before closing a chat', 'close chat confirmation', 'are you sure dialog'],
  'chat.content-filter-fallback-model': ['content filter', 'model refused', 'blocked by content filter', 'safety filter fallback'],
  'chat.content-width': ['chat width', 'wide chat', 'narrow chat', 'message column width', 'full width'],
  'chat.copy-on-select': ['copy selected text', 'auto copy', 'select to copy'],
  'chat.default-memory-mode': ['incognito', 'incognito mode', 'temporary chat', 'persistent memory', 'private chat', "don't remember this chat", 'memory mode for new chats', 'new chats incognito by default', 'never save memory'],
  'chat.default-model': ['model', 'llm', 'opus', 'sonnet', 'haiku', 'gpt', 'default model', 'fallback model', 'switch model', 'which model', 'change model', 'model for new chats', 'model keeps reverting'],
  'chat.default-reasoning-effort': ['reasoning', 'thinking', 'effort', 'thinking depth', 'xhigh', 'reasoning effort', 'how long models think', 'think before answering', 'thinking time'],
  'chat.default-sidebar-folder-for-new-chats': ['new chats folder', 'where new chats go', 'default folder'],
  'chat.describe-your-role': ['about me', 'my job', 'tell the agent who i am', 'role description'],
  'chat.dim-inactive-panes': ['fade other panes', 'dim split view panes', 'highlight active pane'],
  'chat.double-click-to-edit-your-messages': ['edit a sent message', 'edit my message', 'double click edit'],
  'chat.exact-threshold': ['exact compact percent', 'precise compaction threshold', 'compact at exact percent'],
  'chat.fallback-model': ['backup model', 'model when default fails', 'model unavailable', 'retry with another model'],
  'chat.feature-tips': ['tips', 'hints', 'onboarding tips', 'hide tips', 'turn off tips'],
  'chat.file-change-chips': ['changed files chips', 'edited files list', 'file chips'],
  'chat.folder-suggestions': ['suggest a folder', 'folder suggestion for a new chat', 'sort chats into folders'],
  'chat.follow-up-bar-layout': ['suggested replies layout', 'follow up suggestions', 'suggestion chips layout'],
  'chat.hide-empty-folders': ['hide folders with no chats', 'empty folders', 'clean up sidebar folders'],
  'chat.history-expanded': ['expand history', 'history section open', 'show history by default'],
  'chat.include-subfolder-contents-in-folder-counts': ['folder count', 'nested folder count', 'subfolder sessions count'],
  'chat.jump-to-the-bottom-when-you-send': ['scroll to bottom', 'auto scroll', 'stay at the bottom', 'jump to latest message', 'scroll on send'],
  'chat.keep-side-panel-open': ['side panel stays open', 'pin the side panel', 'right panel'],
  'chat.link-previews': ['url previews', 'link cards', 'unfurl links', 'link unfurl'],
  'chat.mcp-apps-in-side-panel': ['mcp ui', 'mcp app panel', 'tool apps in side panel'],
  'chat.merge-queued-messages': ['queue', 'batch', 'combine messages'],
  'chat.message-font-size': ['chat text size', 'bigger chat text', 'smaller chat text', 'message text size'],
  'chat.minimap-location': ['minimap', 'conversation minimap', 'scroll map', 'minimap side'],
  'chat.new-session-folders': ['folder per chat', 'working directory for new chats', 'new chat workspace folder', 'project folder for each chat'],
  'chat.open-sidebar-sessions-in-a-session-tab': ['open chats in tabs', 'session tabs', 'tabbed sessions'],
  'chat.optimize-before-sending': ['improve my prompt', 'rewrite prompt before sending', 'prompt optimizer', 'auto optimize prompt'],
  'chat.optimize-prompt-effort': ['prompt optimizer effort', 'optimize effort'],
  'chat.optimize-prompt-model': ['prompt optimizer model', 'model that rewrites prompts'],
  'chat.pin-forks-of-pinned-sessions': ['pinned forks', 'keep forks pinned', 'fork pin'],
  'chat.pin-summary-in-side-panel': ['session summary panel', 'pin summary', 'summary side panel'],
  'chat.pin-the-latest-turn': ['sticky last turn', 'keep last reply visible', 'pin last message'],
  'chat.plain-diffs': ['simple diffs', 'no syntax highlighting in diffs', 'plain text diff'],
  'chat.pr-and-issue-chips-on-session-cards': ['pull request chips', 'github chips', 'pr badges', 'issue badges'],
  'chat.preferred-reply-language': ['reply language', 'answer in chinese', 'answer in english', 'response language', 'language the agent replies in', 'agent language'],
  'chat.prevent-sleep-while-running': ['keep awake', 'caffeinate', 'stop the computer sleeping', 'no sleep'],
  'chat.preview-lines-on-session-cards': ['session card preview', 'message preview lines', 'snippet lines'],
  'chat.project-tags': ['project folder tag', 'tag sessions by project', 'project labels'],
  'chat.quick-reply-card-on-hover': ['hover reply', 'reply on hover', 'quick reply'],
  'chat.quick-send': ['fast send', 'enter to send', 'hotkey'],
  'chat.reasoning-starts-expanded': ['thinking expanded', 'expand reasoning', 'open thinking by default', 'thinking collapsed'],
  'chat.recall-sent-messages-with-arrow-keys': ['up arrow history', 'previous message', 'arrow up to edit', 'message history'],
  'chat.require-a-second-escape-to-stop-the-turn': ['double escape', 'esc twice to stop', 'accidental stop', 'escape stops the agent'],
  'chat.response-verbosity': ['shorter answers', 'longer answers', 'concise replies', 'verbose replies', 'detailed answers', 'reply length', 'too wordy', 'brief answers'],
  'chat.restore-sessions': ['reopen chats on restart', 'restore open tabs', 'remember open sessions'],
  'chat.restore-window': ['remember window size', 'window position', 'reopen window where it was'],
  'chat.selectable-models': ['model picker list', 'hide models', 'which models show', 'model menu'],
  'chat.send-follow-ups-with-one-click': ['one click follow up', 'click a suggestion to send', 'follow up chips send'],
  'chat.send-shortcut': ['shift enter', 'cmd enter', 'ctrl enter', 'newline instead of send', 'enter sends message'],
  'chat.session-summaries': ['chat summary', 'summarize session', 'summary panel', 'turn off summaries'],
  'chat.sessions-per-creator': ['session limit', 'max sessions per bot', 'too many sessions', 'open session cap'],
  'chat.show-all-bots-in-my-sessions': [
    "show my bots' sessions in my sessions list", 'show bot sessions in the sidebar', 'bot sessions missing from sidebar',
    'bot sessions not showing', 'hide bot sessions', 'bot sessions in my sessions list',
  ],
  'chat.show-context-percentage': ['context usage percent', 'context meter', 'how full is context'],
  'chat.show-context-tokens': ['token count', 'context token count', 'how many tokens'],
  'chat.show-full-titles-while-scrolling': ['long titles', 'full chat title', 'truncated titles'],
  'chat.show-optimize-prompt': ['optimize prompt button', 'prompt optimizer button', 'hide optimize button'],
  'chat.show-pasted-text-in-full': ['large paste', 'pasted text collapsed', 'expand paste'],
  'chat.show-thinking-inline': ['show thinking', 'show thinking in chat', 'show reasoning', 'hide thinking', 'thinking blocks', 'see the model thinking'],
  'chat.show-timestamps': ['time', 'clock', 'message time'],
  'chat.simplified-tool-call-names': ['friendly tool names', 'raw tool names', 'tool call labels'],
  'chat.since-you-left-briefs': [
    'goal needs you doing message', 'catch-up message', 'recap when I come back', 'welcome back summary',
    'turn off the recurring status message', 'stop the bot recap', 'recurring goal needs you doing check-ins message',
    'catch up brief', 'what happened while i was away', 'away summary',
  ],
  'chat.soft-stop-budget-seconds': ['stop timeout', 'graceful stop', 'stop button kills the session'],
  'chat.spell-check-message-input': ['spell check', 'spellcheck', 'spelling', 'red underline', 'squiggle', 'dictionary'],
  'chat.split-side-by-side-diffs': ['split', 'unified', 'split diff view', 'unified diff view', 'diff layout', 'side by side diffs'],
  // General navigation. The split-view button in the chat header shows only
  // while this is on, so a question about the missing button lands here.
  'chat.split-view-session-grid': ['split pane', 'multi session', 'grid view', 'split view', 'split screen', 'side by side sessions', 'session grid', 'two chats at once', 'split view button'],
  'chat.start-new-chats-in-conductor-mode': ['conductor mode', 'turn off conductor mode', 'disable conductor mode', 'conductor by default', 'board for new chats', 'stop conductor mode'],
  'chat.style-markdown-while-typing': ['markdown', 'bold', 'italic', 'strikethrough', 'inline code', 'formatting', 'live preview'],
  'chat.subagent-effort': ['worker effort', 'subagent reasoning effort', 'helper agent effort'],
  'chat.subagent-model': ['worker model', 'model for subagents', 'spawned agent model', 'child agent model', 'cheaper model for workers'],
  'chat.swipe-to-open-and-close-side-panel': ['swipe gesture', 'trackpad swipe side panel', 'swipe the panel'],
  'chat.tail-only-fork': ['fork from here', 'fork keeps later messages', 'branch a chat'],
  'chat.technical-comfort': ['beginner', 'expert', 'how technical am i', 'explain simply', 'skill level'],
  'chat.text-link-patterns': ['auto link', 'linkify', 'ticket links', 'custom link rules', 'jira links'],
  'chat.text-streaming-style': ['typing animation', 'streaming animation', 'text appears word by word', 'smooth streaming'],
  'chat.title-lines-on-session-cards': ['title wrap', 'two line titles', 'session card title lines'],
  'chat.tool-calls-start-expanded': ['expand tool calls', 'tool call details', 'collapse tool calls'],
  'chat.topic-tags': ['auto tags', 'topic labels', 'tag sessions by topic'],
  'chat.turn-time-limit-seconds': ['turn timeout', 'stops after 4 hours', 'long running turn', 'max turn duration', 'agent stopped after a while', 'time limit'],
  'chat.what-enter-does-while-the-agent-is-working': ['steer', 'queue message', 'interrupt the agent', 'send while running', 'steer or queue'],
  'chat.widget-density': ['compact widgets', 'dense layout', 'widget spacing'],
  'chat.your-role': ['my role', 'job title', 'developer or manager', 'who i am'],

  // Computer use
  'computer-use.attach-screenshots': ['screen capture', 'screenshot', 'pixels', 'window image'],
  'computer-use.enable-computer-use': ['accessibility', 'desktop', 'click', 'keyboard', 'screenshot', 'a11y', 'automation', 'gui', 'control my computer'],
  'computer-use.max-tree-nodes': ['accessibility tree size', 'ui tree limit', 'element limit'],
  'computer-use.screenshot-width': ['screenshot size', 'screenshot resolution', 'image width'],
  'computer-use.show-cursor-motion': ['show the mouse', 'cursor animation', 'pointer movement'],

  // Connections
  'connections.oauth-client-asana': ['asana oauth', 'asana client id', 'asana client secret', 'connect asana'],
  'connections.oauth-client-github': ['github oauth', 'github client id', 'github client secret', 'connect github'],
  'connections.oauth-client-google': ['google oauth', 'google client id', 'connect gmail', 'connect google drive', 'google calendar'],
  'connections.oauth-client-slack': ['slack oauth', 'slack client id', 'slack client secret', 'slack oauth app'],
  'connections.remember-mcp-sign-ins-across-sessions': ['stay signed in to mcp', 'mcp oauth tokens', 'mcp asks to sign in again', 'mcp login'],

  // Developer
  'developer.acp-runtime-sharing': ['shared runtime', 'save memory', 'one process for many chats', 'acp pool'],
  'developer.agent-harness-for-worker-chats': ['claude for workers', 'codex workers', 'worker harness', 'different engine for workers'],
  'developer.also-send-screenshots-of-the-agent-s-browser-tab': ['jev screenshots', 'decision model screenshots', 'browser screenshots off machine'],
  'developer.also-send-snippets-of-recalled-memories-so-jev-can-drop-the-ones-that-do-not-help': ['jev memory snippets', 'memory recall snippets', 'decision model memories'],
  'developer.also-send-the-conversation-and-tool-call-inputs-so-jev-can-score-compaction': ['jev compaction scoring', 'score compaction', 'decision model compaction'],
  'developer.also-send-tool-call-arguments-so-jev-can-flag-risky-calls': ['jev risky calls', 'flag risky tool calls', 'tool call risk'],
  'developer.app-browser-access': ['apps use the browser', 'app browser tabs', 'let apps read the browser'],
  'developer.app-tools-in-bot-chats': ['app tools for bots', 'apps in bot chats', 'bot app tools'],
  'developer.artifact-deploy': ['deploy an artifact', 'publish artifact', 'host artifact on aws', 'share artifact publicly'],
  'developer.automatic-cards-for-all-sessions': ['session cards', 'all dashboards', 'auto cards', 'next steps cards'],
  'developer.chat-on-a-crew': ['remote crew chat', 'chat on another machine', 'connected crew'],
  'developer.connected-computers': ['my laptop folder', 'work on my computer', 'local folder from server', 'remote computer'],
  'developer.decisions': ['decision model', 'jev', 'auto decisions'],
  'developer.earlier-conversation-one-decision-may-carry-in-characters': ['decision context size', 'jev context characters', 'how much conversation jev sees'],
  'developer.explore': ['explore menu', 'try features', 'experimental features', 'explore in top bar'],
  'developer.folder-conductor': ['folder parent chat', 'conductor at top of folder', 'folder lead chat'],
  'developer.jev-api-key': ['jev key', 'decision model key', 'jev token'],
  'developer.kas-sub-agents': ['kas subagents', 'engine sub-agents', 'in-engine subagents'],
  'developer.let-agents-control-your-other-sessions': ['agents message other sessions', 'cross session control', 'agents close my chats', 'session control tools'],
  'developer.mark-out-of-date-dashboard-fields': ['stale fields', 'outdated dashboard', 'stale dashboard fields'],
  'developer.mcp-sign-in-by-kiro-crew': ['mcp oauth', 'connect github mcp', 'mcp server login', 'sign in to mcp servers'],
  'developer.model-for-the-small-model-judge': ['judge model', 'small judge model', 'cheap judge'],
  'developer.phone-widgets': ['mobile widgets', 'home screen widgets', 'kiro mobile widgets', 'ios widgets'],
  'developer.quick-worker-check-before-a-hard-choice': ['second opinion', 'worker check', 'hard choice brief', 'quick check workers'],
  'developer.remote-dispatch': ['run tasks on other machines', 'remote workers', 'dispatch to connected machines'],
  'developer.reply-threads-on-bot-chat-messages': ['threads in bot chat', 'reply in a thread', 'bot message threads'],
  'developer.run-a-local-gateway': ['local gateway', 'start the gateway', 'gateway on this computer', 'app cannot connect'],
  'developer.send-pending-approvals-to-chat-apps': ['approvals in slack', 'approve from slack', 'approval notifications in chat apps'],
  'developer.session-environments': ['remote workspace', 'cloud workspace', 'coder', 'sprites', 'run a chat in a container'],
  'developer.shared-crew': ['share my crew', 'invite people', 'people tab', 'team crew', 'multi user'],
  'developer.use-your-company-s-sign-in': ['sso', 'company login', 'single sign-on', 'idc portal'],
  'developer.visual-refresh': ['new look', 'new design', 'redesign', 'new ui'],
  'developer.wake-bots-to-update-out-of-date-fields': ['refresh stale fields', 'wake bots', 'update dashboard fields'],
  'developer.webhooks': ['webhook', 'incoming webhook', 'http hook', 'external triggers'],
  'developer.which-judge-answers': ['judge', 'pick a judge', 'judge choice', 'small model or decision model'],

  // Display
  'display.command-completion': ['terminal autocomplete', 'shell completion', 'tab completion'],
  'display.copy-on-select': ['terminal copy on select', 'select to copy in terminal'],
  'display.ctrl-c-and-ctrl-v-copy-and-paste': ['terminal copy paste', 'paste in terminal', 'ctrl c interrupts'],
  'display.cursor-style': ['block cursor', 'bar cursor', 'underline cursor', 'terminal cursor', 'caret'],
  'display.custom-font': ['my own font', 'install a font', 'font file', 'custom typeface'],
  'display.dark-from': ['dark mode schedule', 'dark at night', 'auto dark mode time'],
  'display.default-for-new-sessions': ['session color', 'chat color', 'color coding', 'auto cycle colors'],
  'display.default-shell': ['bash', 'zsh', 'fish', 'powershell', 'terminal shell', 'change shell'],
  'display.display-mode': ['sidebar colors', 'session colors in sidebar', 'colored sidebar'],
  'display.editor': ['open in vs code', 'vscode', 'cursor editor', 'open file in editor', 'remote ssh editor'],
  'display.enable-ligatures': ['font ligatures', 'code ligatures', 'arrow ligatures'],
  'display.explore': ['explore button', 'milestones', 'explore in the sidebar'],
  'display.external-terminal-app': ['iterm', 'terminal app', 'open in terminal', 'warp', 'ghostty'],
  'display.font': ['terminal font', 'terminal typeface'],
  'display.font-family': ['typeface', 'monospace', 'sans-serif', 'change font'],
  'display.font-size': ['terminal text size', 'bigger terminal text', 'smaller terminal text'],
  'display.highlight-recent-sessions': ['recent chats highlight', 'highlight new sessions', 'recently used sessions'],
  'display.intensity': ['sidebar color strength', 'vivid colors', 'soft colors', 'color intensity'],
  'display.interface': ['chat mode', 'cli mode', 'bubbles', 'terminal'],
  // What users call the behaviour, whatever their platform names the tray.
  'display.keep-running-in-the-background-after-closing-the-window': ['close to tray', 'quit on close', 'exit on close', 'run in background', 'system tray', 'close button'],
  'display.language': ['ui language', 'dashboard language', 'change language', 'interface language', 'chinese', 'japanese', 'translate the app'],
  'display.light-from': ['light mode schedule', 'light in the day', 'auto light mode time'],
  'display.minimize-to-tray': ['minimize to system tray', 'hide in tray', 'menu bar'],
  'display.mode': ['dark mode', 'light mode', 'theme', 'appearance', 'dark', 'light', 'night', 'night mode', 'dark theme', 'light theme'],
  'display.overlays': ['theme overlays', 'background effects', 'visual effects'],
  'display.palette': ['sidebar palette', 'color palette for sessions', 'categorical colors'],
  'display.persona': ['theme persona', 'theme character', 'mascot'],
  'display.reopen-where-i-left-off': ['restore last page', 'open where i was', 'resume last view'],
  'display.reuse-the-current-terminal': ['same terminal window', 'reuse terminal tab', 'copy command to terminal'],
  'display.reverse-trackpad-swipe': ['natural scrolling', 'invert swipe', 'swipe direction'],
  'display.show-channels': ['hide channels', 'channels in left rail', 'remove channels from sidebar'],
  'display.show-search-in-the-top-bar': ['search bar', 'top bar search', 'hide search box'],
  'display.show-task-runner': ['hide task runner', 'task runner in left rail', 'turn off task runner'],
  'display.show-tray-icon': ['tray icon', 'menu bar icon', 'hide tray icon', 'system tray icon'],
  'display.show-unread-dot-on-session-cards': ['unread badge', 'unread indicator', 'blue dot'],
  'display.sounds': ['theme sounds', 'ui sounds', 'click sounds'],
  'display.ssh-host': ['remote ssh host', 'ssh config host', 'dev box name'],
  'display.theme': ['theme', 'appearance', 'skin', 'palette', 'color theme', 'colour theme', 'colors', 'colours', 'change colors', 'change colours', 'app appearance', 'color scheme', 'colour scheme'],
  'display.touch-screen-layout': ['tablet layout', 'touch mode', 'ipad layout', 'bigger tap targets'],
  'display.translucent-panels': ['transparency', 'blur', 'see-through panels', 'frosted glass'],
  'display.use-command-bar': ['cmd k', 'ctrl k', 'command palette', 'quick open'],
  'display.zoom-level': ['magnification', 'scale', 'bigger', 'smaller', 'bigger text', 'smaller text', 'accessibility', 'zoom in', 'zoom out'],

  // Instances
  'instances.auto-connect-crews': ['connect remote crews on start', 'auto connect remote crew', 'reconnect crews'],
  'instances.crews-kept-loaded': ['warm crews', 'how many crews stay loaded', 'loaded remote crews'],
  'instances.enable-remote-crew-management': ['remote crews', 'add a remote crew', 'cloud desktop crew', 'remote crew management is disabled'],
  'instances.remote-control': ['use from my phone', 'kiro mobile', 'reach crew from other devices', 'relay', 'turn off remote control'],

  // Agent Harness. fuzzyMatch is literal, so the spaced, hyphenated and joined
  // spellings of "log out" are separate entries.
  'kiro-account.kiro-sign-in': ['log out', 'logout', 'log-out', 'signout', 'sign-out', 'account', 'change account', 'another account', 'login', 'log in', 'credits', 'balance', 'usage', 'plan', 'not signed in to kiro'],

  // Notifications
  'notifications.mark-sessions-unread-only-when-they-need-you': ['unread only when needed', 'fewer unread chats', 'unread badge rules'],
  'notifications.mute-sounds': ['silence all sounds', 'no sounds', 'turn off sounds'],
  'notifications.notify-me-even-when-kiro-crew-is-closed': ['push notifications', 'notifications when closed', 'background notifications'],
  'notifications.notify-when-a-background-chat-finishes': ['chat done notification', 'tell me when a chat finishes', 'background chat alert'],
  'notifications.only-notify-for-longer-turns': ['skip quick turn alerts', 'notify only for long turns', 'fewer notifications'],
  'notifications.play-sound-on-new-notifications': ['alert', 'audio', 'mute', 'silent', 'notification sound', 'ding', 'turn off notification sound', 'beep'],
  'notifications.play-sounds-only-during-set-hours': ['quiet hours', 'do not disturb', 'night mode for sounds', 'sound schedule'],
  'notifications.show-a-banner-while-this-window-is-open': ['in-app banner', 'toast', 'popup notification'],
  'notifications.sound-category-agent': ['agent message sound', 'proactive message sound'],
  'notifications.sound-category-all': ['default notification sound', 'fallback sound'],
  'notifications.sound-category-approval': ['approval sound', 'question sound', 'needs approval sound'],
  'notifications.sound-category-cron': ['scheduled job sound', 'cron sound', 'reminder sound'],
  'notifications.sound-category-dictation': ['dictation stopped sound', 'dictation interrupted sound'],
  'notifications.sound-category-heartbeat': ['heartbeat sound', 'heartbeat result sound'],
  'notifications.sound-category-hook': ['webhook sound', 'hook trigger sound'],
  'notifications.sound-category-skills': ['skill candidate sound', 'new skill sound'],
  'notifications.sound-category-subagent': ['subagent done sound', 'worker finished sound'],
  'notifications.sound-category-taskrunner': ['task done sound', 'task runner sound'],
  'notifications.sound-category-turn': ['turn finished sound', 'reply done sound', 'handoff sound'],
  // The lasting way to silence a kind of notification (the keep-or-mute prompt
  // under a new channel's first notification is one-time).
  'notifications.sources': ['mute', 'priority', 'channel', 'per-app', 'silence', 'turn off notifications', 'turn off these notifications', 'mute a notification source', 'stop getting notifications', 'alert on every session close'],
  'notifications.volume': ['loudness', 'sound level', 'quieter', 'louder'],

  // Privacy
  'privacy.record-metrics': ['metrics', 'local metrics', 'usage metrics', 'telemetry'],
  'privacy.send-anonymous-usage-heartbeat': ['telemetry', 'analytics', 'phone home', 'opt out of tracking', 'usage ping'],

  // Secrets
  'secrets.jira-api-token': ['jira token', 'jira key', 'connect jira'],
  'secrets.wakatime-api-key': ['wakatime key', 'wakatime token', 'coding time tracking'],

  // Security
  'security.ask-me-before-running-tools-set-to-auto-approve-in-a-tool-server-s-settings': ['mcp', 'autoApprove', 'auto approve', 'honour_auto_approve', 'pre-approved tools', 'tools a server marks as safe', 'tools an MCP server pre-approves'],
  'security.config-pending': ['config.json edited', 'settings changed outside', 'pending config changes', 'apply config edits'],
  'security.denied-commands': ['deny', 'denylist', 'blocklist', 'blocked commands', 'command rules', 'command blocked'],
  'security.governance-policy': ['policy', 'admin policy', 'security policy', 'governance'],
  'security.how-long-auto-approve-stays-on': ['yolo', 'yolo mode', 'auto-approve', 'auto approve', 'skip permissions', 'dangerously skip permissions', 'permissions', 'ttl', 'duration', 'expiry', 'until shutdown'],
  'security.live-security-posture': ['security status', 'posture', 'security check', 'security overview'],
  'security.redact-credentials': ['redaction', 'hide secrets', 'mask tokens', 'redacted links', 'turn off redaction', 'censor passwords'],
  'security.stop-the-agent-from-editing-its-own-permission-rules': ['kiro-cli', 'permissions.yaml', 'permission rules', 'protect_kiro_permission_rules', 'changing its own permission rules'],
  'security.trust-every-third-party-app': ['apps', 'third party', 'auto approve apps', 'trust'],
  'security.trust-this-machine-s-tailnet-name': ['lan', 'network', 'remote', 'expose', 'tailscale', 'origin', 'tailscale name', 'open from another device', 'lan access', 'trusted origin'],
  'security.your-custom-denies': ['my deny rules', 'custom deny regex', 'block a command', 'deny pattern'],

  // Session environments
  'session-environments.coder-url': ['coder server', 'coder address', 'coder deployment url'],
  'session-environments.default-for-new-chats': ['default environment', 'new chats run remotely', 'default workspace'],
  'session-environments.machine-size': ['instance size', 'cpu and memory', 'bigger machine', 'workspace size'],
  'session-environments.secret-name-for-the-token-usually-sprites-token': ['sprites token', 'sprites secret', 'environment token'],
  'session-environments.start-new-chats-on-a-profile': ['environment profile', 'workspace profile', 'start chats remotely'],
  'session-environments.stop-workspaces-when-kiro-crew-shuts-down': ['stop remote workspaces', 'shut down workspaces', 'stop billing on exit'],

  // Shortcuts
  'shortcuts.enable-shortcuts': ['keyboard shortcuts', 'hotkeys', 'key bindings', 'turn off shortcuts'],
  'shortcuts.search-everywhere': ['search shortcut', 'double shift', 'search hotkey'],
  'shortcuts.use-ctrl-not-option-for-chat-1-9': ['switch chats with number keys', 'ctrl 1', 'option 1', 'chat number shortcuts'],

  // Skills
  'skills.auto-generate-skills-from-sessions': ['learn skills', 'skill suggestions', 'make skills from chats', 'auto skills'],
  'skills.extra-skill-paths': ['skill folders', 'load skills from folder', 'custom skills directory'],
  'skills.require-approval-before-generated-skills-go-live': ['approve skills', 'review new skills', 'skill approval'],

  // Voice. Dictation (speech to text) rows come from sttSettings, read-aloud
  // (text to speech) rows from voicePanel; a "-2" id is the second of two
  // settings that share a label.
  'voice.auto-speak-responses': ['tts', 'speak', 'read aloud', 'narrate', 'text-to-speech', 'automatic speech', 'auto read', 'read replies aloud', 'speak every answer', 'turn off read aloud'],
  'voice.auto-submit-when-i-finish-speaking': ['send after speaking', 'auto send dictation', 'hands free'],
  'voice.aws-profile-amazon-polly': ['polly profile', 'polly credentials', 'text to speech aws profile'],
  'voice.aws-profile-transcribe': ['transcribe profile', 'transcribe credentials', 'speech to text aws profile'],
  'voice.aws-region-amazon-polly': ['polly region', 'text to speech region'],
  'voice.aws-region-transcribe': ['transcribe region', 'speech to text region'],
  'voice.custom-model-file': ['whisper model file', 'own speech model', 'local model file'],
  'voice.custom-vocabulary': ['dictation words', 'names dictation gets wrong', 'transcription vocabulary', 'custom words'],
  'voice.decode-threads': ['dictation cpu threads', 'whisper threads', 'transcription speed'],
  'voice.dictation-panel': ['dictation window', 'transcript panel', 'show what i said'],
  'voice.enabled': ['dictation', 'turn on dictation', 'turn off dictation', 'voice input', 'speech to text on'],
  'voice.engine': ['speech engine', 'tts engine', 'read aloud engine'],
  'voice.file-fingerprint-sha-256': ['model checksum', 'sha256', 'verify model file'],
  'voice.hold-the-message-box-to-talk': ['press and hold to talk', 'hold to dictate', 'long press mic'],
  'voice.hold-time': ['long press time', 'hold duration', 'press and hold delay'],
  'voice.how-the-key-works': ['push to talk', 'hold to talk', 'toggle dictation', 'press on off'],
  'voice.language': ['stt', 'dictation', 'microphone', 'whisper', 'transcribe', 'speech-to-text', 'dictation language'],
  'voice.language-2': ['read aloud language', 'tts language', 'spoken reply language'],
  'voice.microphone': ['mic', 'input device', 'pick a microphone', 'headset mic', 'microphone permission'],
  'voice.mixed-languages': ['code switching', 'two languages in one sentence', 'bilingual dictation'],
  'voice.model': ['speech recognition model', 'whisper model', 'dictation model'],
  'voice.provider': ['dictation provider', 'speech to text service', 'amazon transcribe', 'local whisper'],
  'voice.provider-2': ['text to speech provider', 'amazon polly', 'tts provider', 'built-in voice'],
  'voice.send-when-i-stop-recording': ['send when recording stops', 'auto send voice message'],
  'voice.shortcut-key': ['voice hotkey', 'dictation hotkey', 'push to talk key', 'right option key', 'mic shortcut'],
  'voice.slide-up-and-right-to-send': ['swipe to send voice', 'slide to send', 'mobile voice gesture'],
  'voice.speed': ['speech speed', 'talk faster', 'talk slower', 'reading speed'],
  'voice.speed-2': ['tts speed', 'read aloud speed', 'speech rate'],
  'voice.streaming': ['live transcription', 'words appear as i talk', 'streaming dictation'],
  'voice.tap-vs-hold-cutoff': ['tap or hold', 'tap time', 'hold threshold'],
  'voice.tell-the-agent-this-was-dictated': ['mark dictated messages', 'voice message note', 'tell the agent i spoke'],
  'voice.tidy-up-transcripts-with-ai': ['clean up dictation', 'fix transcript', 'remove filler words', 'polish dictation'],
  'voice.use-a-key-to-start-dictation': ['dictation shortcut', 'turn off the dictation key', 'option key asks for microphone', 'keyboard dictation'],
  'voice.voice': ['voice choice', 'pick a voice', 'speaker voice', 'tts voice'],
  'voice.voice-2': ['read aloud voice', 'narrator voice', 'polly voice'],
}

/**
 * One-word SETTINGS_KEYWORDS entries that find_ui keeps for a setting. find_ui
 * answers a whole one-word question with confidence, so list a word here only
 * when a bare "colors" should mean this setting and nothing else. Every word
 * must also be in that setting's SETTINGS_KEYWORDS list (the generator fails
 * otherwise, and on an unknown id); the other find_ui filters still apply.
 */
export const GUIDE_ONE_WORD_TERMS: Record<string, string[]> = {
  'display.theme': ['colors', 'colours', 'appearance'],
}
