/**
 * Registered UI locations on the Schedule page. One area of `UI_LOCATIONS`;
 * see `../descriptors.ts` for the two-step contract.
 *
 * Creating a job has two entry points that are never on screen together: the
 * empty state's "Create your first job" while there is no job, and the toolbar's
 * Add Job (the main half of `AddJobSplitButton`) once there is one. Both carry
 * the same newcomer words, each qualified by the state that shows it.
 */
import type { UiLocationArea } from '../types'

const CREATE_TERMS = {
  en: [
    'make a schedule', 'create a schedule', 'new schedule', 'new scheduled job', 'schedule a task',
    'create a job', 'new job', 'add a cron job', 'create a reminder', 'make a reminder', 'set a reminder',
    'new reminder', 'recurring task',
  ],
  'zh-CN': ['新建定时任务', '创建定时任务', '添加定时任务', '新建提醒', '创建提醒', '设置提醒', '新建计划任务', '定一个提醒'],
  ja: ['スケジュールを作成', '新しいジョブ', 'リマインダーを作成'],
  ko: ['일정 만들기', '새 작업', '알림 만들기'],
  es: ['crear una programación', 'nueva tarea programada', 'crear un recordatorio'],
  fr: ['créer une planification', 'nouvelle tâche planifiée', 'créer un rappel'],
  de: ['Zeitplan erstellen', 'neuer geplanter Job', 'Erinnerung erstellen'],
  pt: ['criar um agendamento', 'nova tarefa agendada', 'criar um lembrete'],
  it: ['creare una pianificazione', 'nuova attività pianificata', 'creare un promemoria'],
  ru: ['создать расписание', 'новое задание', 'создать напоминание'],
} as const

export const LOCATIONS = {
  // The job table (List view): the picker a guide's "choose the job" step
  // points at (UI_SELECTION_SCOPES.job_open). Opening a row opens its panel.
  'schedule.job-list': {
    kind: 'list',
    label: { from: 'attr', attr: 'aria-label' },
    guide: false,
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'schedule_list_view' }],
    }],
  },
  'schedule.create-first': {
    kind: 'button',
    terms: CREATE_TERMS,
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'no_schedules' }],
    }],
  },
  'schedule.add-job': {
    kind: 'button',
    terms: CREATE_TERMS,
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'has_schedules' }],
    }],
  },
  // The empty state's gallery button. Once a job exists the same gallery sits
  // behind Add Job's caret (AddJobSplitButton), which is not registered here.
  'schedule.templates': {
    kind: 'button',
    terms: {
      en: ['job templates', 'schedule templates', 'pre-made schedules', 'example schedules', 'schedule presets'],
      'zh-CN': ['任务模板', '定时任务模板', '预设任务', '现成的定时任务'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'no_schedules' }],
    }],
  },
  // New folder, Select all and the filter row are drawn only in the List view,
  // which is the page's default (SchedulePage: `jobsView === 'list'`); the
  // Calendar and Executions views hide them.
  'schedule.new-folder': {
    kind: 'button',
    terms: {
      en: ['schedule folder', 'job folder', 'group my jobs', 'organize scheduled jobs'],
      'zh-CN': ['任务文件夹', '定时任务文件夹', '整理定时任务'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'has_schedules' }, { kind: 'condition', id: 'schedule_list_view' }],
    }],
  },
  // The header checkbox of the jobs table: the way to act on every job at
  // once, so the batch move's words are here, never on the move button.
  'schedule.select-all': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['select every job', 'check all jobs', 'select multiple jobs', 'move all jobs to a folder', 'move several jobs to a folder at once'],
      'zh-CN': ['全选任务', '批量选择任务', '把所有任务移到文件夹', '批量移动任务到文件夹'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'has_schedules' }, { kind: 'condition', id: 'schedule_list_view' }],
    }],
  },
  // The job detail dialog's controls: the dialog opens by selecting a job in
  // the list, so each hangs under the page with `job_open`. Run Now and Cancel
  // Run share one slot: Run Now is drawn while the job is idle, Cancel Run
  // while it runs.
  // "Run a job now" is the row's own Run (schedule.row-run), on screen without
  // opening the job; this is the same run from the open job's panel.
  'schedule.run-now': {
    kind: 'button',
    terms: {
      en: ['run now in the job panel', 'run the open job now'],
      'zh-CN': ['在任务详情里立即运行', '运行打开的任务'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'job_open' }, { kind: 'condition', id: 'job_not_running' }],
    }],
  },
  // The job row's own Run button (List view), next to its name: the same
  // run as the panel's Run Now, without opening the job first.
  'schedule.row-run': {
    kind: 'button',
    label: { from: 'text', key: 'pages.schedulePage.run' },
    terms: {
      en: [
        'run a job now', 'run my schedule immediately', 'trigger a job', 'test run a job', 'start a job manually',
        'run this job from the list', 'run button on a job row',
      ],
      'zh-CN': ['手动运行任务', '马上执行任务', '触发定时任务', '在列表里运行任务', '任务行的运行按钮'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'has_schedules' }, { kind: 'condition', id: 'schedule_list_view' }],
    }],
  },
  'schedule.cancel-run': {
    kind: 'button',
    terms: {
      en: ['stop a running job', 'cancel a running job', 'abort a job', 'kill a job'],
      'zh-CN': ['停止运行中的任务', '终止任务', '中止定时任务'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'job_open' }, { kind: 'condition', id: 'job_running' }],
    }],
  },
  'schedule.delete': {
    kind: 'button',
    terms: {
      en: ['delete a scheduled job', 'remove a scheduled job', 'delete a job', 'delete a reminder', 'remove a cron job'],
      'zh-CN': ['删除定时任务', '删除任务', '删除提醒', '移除定时任务'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'job_open' }],
    }],
  },
  // A credential decision: no guide binding, ever. Drawn in the panel's
  // Details tab (JobSecretsPanel); the Logs tab replaces that body.
  'schedule.secret-approve': {
    kind: 'button',
    terms: {
      en: ['approve a secret for a job', 'approve secret request', 'grant a job a secret', 'allow a job to use a secret'],
      'zh-CN': ['批准密钥', '批准密钥请求', '允许任务使用密钥'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [
        { kind: 'condition', id: 'job_open' },
        { kind: 'condition', id: 'job_details_tab' },
        { kind: 'condition', id: 'job_secret_request_pending' },
      ],
    }],
  },
  // The job panel's Pause / Resume (one button whose label follows the job's
  // state). The row's ⋯ menu repeats it; not registered.
  'schedule.pause': {
    kind: 'toggle',
    label: { from: 'text', key: 'pages.schedulePage.pause' },
    aliasKeys: ['pages.schedulePage.resume'],
    stateLabels: [
      { key: 'pages.schedulePage.pause', when: 'job_enabled' },
      { key: 'pages.schedulePage.resume', when: 'job_paused' },
    ],
    terms: {
      en: [
        'pause a scheduled job', 'pause a job', 'pause a reminder', 'resume a scheduled job', 'resume a job',
        'temporarily stop a job', 'turn off a scheduled job for now', 'disable a scheduled job', 'turn a job back on',
      ],
      'zh-CN': ['暂停定时任务', '暂停任务', '暂停提醒', '恢复定时任务', '继续定时任务', '暂时停止任务', '停用定时任务'],
      ja: ['ジョブを一時停止', 'ジョブを再開'],
      ko: ['작업 일시 중지', '작업 재개'],
      es: ['pausar una tarea programada', 'reanudar una tarea'],
      fr: ['suspendre une tâche planifiée', 'reprendre une tâche'],
      de: ['geplanten Job pausieren', 'Job fortsetzen'],
      pt: ['pausar uma tarefa agendada', 'retomar uma tarefa'],
      it: ['mettere in pausa un’attività pianificata', 'riprendere un’attività'],
      ru: ['приостановить задание', 'возобновить задание'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'job_open' }],
    }],
  },
  // The job form's Schedule picker (JobForm), which sets when and how often a
  // job runs; editing an existing job means opening it and changing this. The
  // crewmate editor's Schedules pane draws the same form for a new wake-up;
  // not registered.
  'schedule.edit-when': {
    kind: 'field',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: [
        'edit a scheduled job', 'edit a job', 'change a scheduled job', 'change when a job runs',
        'change the time a job runs', 'reschedule a job', 'change how often a job runs', 'edit a reminder',
      ],
      'zh-CN': ['编辑定时任务', '修改定时任务', '修改执行时间', '更改运行时间', '改提醒时间', '调整执行频率', '重新安排任务'],
      ja: ['ジョブを編集', '実行時間を変更'],
      ko: ['작업 편집', '실행 시간 변경'],
      es: ['editar una tarea programada', 'cambiar la hora de ejecución'],
      fr: ['modifier une tâche planifiée', "changer l'heure d'exécution"],
      de: ['geplanten Job bearbeiten', 'Ausführungszeit ändern'],
      pt: ['editar uma tarefa agendada', 'mudar o horário de execução'],
      it: ['modificare un’attività pianificata', 'cambiare l’orario di esecuzione'],
      ru: ['изменить задание', 'изменить время запуска'],
    },
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'content',
      requires: [{ kind: 'condition', id: 'job_open' }, { kind: 'condition', id: 'job_details_tab' }],
    }],
  },
  // The page header's Executions view (a segment of the view switcher): every
  // job's past runs with status, time and output. The job panel's Logs tab
  // shows one job's runs; not registered.
  'schedule.view-executions': {
    kind: 'tab',
    terms: {
      en: [
        'run history', 'job run history', 'past runs', 'did my job fail', 'failed runs', 'job results',
        'when did my job last run', 'scheduled job log',
      ],
      'zh-CN': ['运行记录', '运行历史', '任务历史', '执行历史', '任务有没有失败', '失败的任务', '定时任务结果'],
      ja: ['ジョブの実行履歴', '失敗した実行'],
      ko: ['작업 실행 기록', '실패한 실행'],
      es: ['historial de ejecuciones', 'ejecuciones fallidas'],
      fr: ['historique des exécutions', 'exécutions échouées'],
      de: ['Ausführungsverlauf', 'fehlgeschlagene Läufe'],
      pt: ['histórico de execuções', 'execuções com falha'],
      it: ['cronologia delle esecuzioni', 'esecuzioni non riuscite'],
      ru: ['история запусков', 'неудачные запуски'],
    },
    placements: [{ surface: 'schedule', parent: 'page.schedule', entry: 'toolbar' }],
  },
  // The list's batch Move to folder (a folder button), drawn once jobs are checked.
  'schedule.move-to-folder': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['move a job to a folder', 'put a scheduled job in a folder', 'file a job in a folder', 'organize jobs into folders'],
      'zh-CN': ['把任务移到文件夹', '移动定时任务到文件夹', '给任务归类到文件夹', '任务放进文件夹'],
    },
    // Drawn once a job is checked, and it moves EVERY checked job: the guide
    // first has the person tick the one job they mean (its own box, a select
    // step that takes a pick) and goes on only while that job alone is
    // checked. A move of every job is Select all's (above).
    placements: [{
      surface: 'schedule', parent: 'page.schedule', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'schedule_list_view' }, { kind: 'condition', id: 'one_job_checked' }],
    }],
  },
} as const satisfies UiLocationArea
