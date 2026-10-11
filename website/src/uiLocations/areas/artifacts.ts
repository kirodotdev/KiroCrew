/**
 * Registered UI locations on the Artifacts page. One area of `UI_LOCATIONS`;
 * see `../descriptors.ts` for the two-step contract.
 */
import { PREVIEW_ARTIFACT_DEPLOY } from '../../utils/previewFlags'
import type { UiLocationArea } from '../types'

export const LOCATIONS = {
  // The labelled half of the header's split button. Its children are a
  // spinner-or-icon expression plus the label, so the key is named.
  'artifacts.new': {
    kind: 'button',
    label: { from: 'text', key: 'pages.artifactsPage.new_artifact' },
    aliasKeys: ['pages.artifactsPage.start_a_new_blank_document_in_the_library'],
    terms: {
      en: ['new document', 'create a document', 'blank document', 'new doc', 'create an artifact', 'write a document'],
      'zh-CN': ['新建文档', '创建文档', '空白文档', '新文档', '创建产物'],
      ja: ['新しいドキュメント', 'ドキュメントを作成'],
      ko: ['새 문서', '문서 만들기'],
      es: ['nuevo documento', 'crear un documento'],
      fr: ['nouveau document', 'créer un document'],
      de: ['neues Dokument', 'Dokument erstellen'],
      pt: ['novo documento', 'criar um documento'],
      it: ['nuovo documento', 'creare un documento'],
      ru: ['новый документ', 'создать документ'],
    },
    placements: [{ surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar' }],
  },
  // The split button's caret. Its accessible name depends on the viewport: on
  // a phone the same menu also holds New folder and reads "More actions". Only
  // the desktop name is registered, so the phone placement is not indexed.
  'artifacts.add-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label', key: 'pages.artifactsPage.more_ways_to_add_an_artifact' },
    placements: [{
      surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar',
      requires: [{ kind: 'viewport', value: 'desktop' }],
    }],
  },
  'artifacts.import': {
    kind: 'menu-item',
    terms: {
      en: ['upload a file', 'import a file', 'upload', 'add a file', 'upload a document', 'import a document'],
      'zh-CN': ['上传文件', '导入文件', '上传', '上传文档', '导入文档'],
      ja: ['ファイルをアップロード', 'ファイルをインポート'],
      ko: ['파일 업로드', '파일 가져오기'],
      es: ['subir un archivo', 'importar un archivo'],
      fr: ['téléverser un fichier', 'importer un fichier'],
      de: ['Datei hochladen', 'Datei importieren'],
      pt: ['enviar um arquivo', 'importar um arquivo'],
      it: ['caricare un file', 'importare un file'],
      ru: ['загрузить файл', 'импортировать файл'],
    },
    placements: [{ surface: 'artifacts', parent: 'artifacts.add-menu', entry: 'menu' }],
  },
  // The Starred half of the Starred/All filter. One element, placed in the
  // toolbar on a desktop and beside the view switcher on a phone.
  'artifacts.starred': {
    kind: 'toggle',
    aliasKeys: ['pages.artifactsPage.filter_starred'],
    terms: {
      en: ['starred artifacts', 'my starred documents', 'favorite artifacts', 'pinned artifacts', 'show only starred', 'favorite documents', 'saved documents'],
      'zh-CN': ['已加星标的产物', '加星的产物', '收藏的产物', '只看已加星', '收藏的文档', '收藏文档', '加星的文档'],
    },
    placements: [{ surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar' }],
  },
  // The desktop toolbar's New folder. On a phone it moves into the add menu,
  // whose phone name ("More actions") is not registered, so that copy is not
  // indexed.
  'artifacts.new-folder': {
    kind: 'button',
    aliasKeys: ['pages.artifactsPage.create_a_folder_to_organize_your_artifacts'],
    terms: {
      en: ['make an artifact folder', 'create an artifact folder', 'new artifact folder', 'organize my artifacts', 'organize my documents'],
      'zh-CN': ['新建产物文件夹', '产物文件夹', '整理产物'],
    },
    placements: [{
      surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar',
      requires: [{ kind: 'viewport', value: 'desktop' }],
    }],
  },
  // The desktop toolbar's link to the Artifact Deploy console, behind its
  // preview flag. On a phone it moves into the add menu, whose phone name is
  // not registered (see artifacts.add-menu), so that copy is not indexed.
  'artifacts.deploy': {
    kind: 'button',
    label: { from: 'text', key: 'pages.artifactsPage.artifact_deploy' },
    aliasKeys: ['pages.artifactsPage.artifact_deploy_aws_profiles_and_published_sites'],
    terms: {
      en: ['publish an artifact', 'deploy an artifact', 'publish to the web', 'share a public link', 'host my page', 'publish a public link', 'make a public link'],
      'zh-CN': ['发布产物', '部署产物', '发布到网上', '公开链接'],
    },
    placements: [{
      surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar',
      requires: [
        { kind: 'viewport', value: 'desktop' },
        { kind: 'preview_flag', flag: PREVIEW_ARTIFACT_DEPLOY },
        { kind: 'condition', id: 'cloud_deploy_available' },
      ],
    }],
  },
  // The library's card gallery: the picker a guide's "choose the artifact"
  // step points at (UI_SELECTION_SCOPES.artifact_open). Each card carries the
  // artifact's name, and opening one makes the pick on the artifact's page.
  'artifacts.list': {
    kind: 'list',
    label: { from: 'attr', attr: 'aria-label', key: 'pages.artifactsPage.your_artifacts' },
    guide: false,
    placements: [{ surface: 'artifacts', parent: 'page.artifacts', entry: 'content' }],
  },
  // An artifact's own page lives at `/artifacts/:slug`, a route that needs an
  // artifact, so its controls hang under the library a person opens it from
  // (`artifact_open`). The toolbar's Version picker lists the saved versions;
  // choosing an older one shows it with a Revert button beside the picker.
  'artifacts.detail.versions': {
    kind: 'field',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: [
        'artifact versions', 'version history', 'restore an earlier version', 'go back to a previous version',
        'older version of a document', 'roll back an artifact', 'undo changes to a document',
      ],
      'zh-CN': ['历史版本', '版本历史', '恢复以前的版本', '恢复旧版本', '回退版本', '产物版本', '文档的旧版本'],
      ja: ['バージョン履歴', '以前のバージョンに戻す'],
      ko: ['버전 기록', '이전 버전으로 되돌리기'],
      es: ['historial de versiones', 'restaurar una versión anterior'],
      fr: ['historique des versions', 'restaurer une version précédente'],
      de: ['Versionsverlauf', 'frühere Version wiederherstellen'],
      pt: ['histórico de versões', 'restaurar uma versão anterior'],
      it: ['cronologia delle versioni', 'ripristinare una versione precedente'],
      ru: ['история версий', 'восстановить прежнюю версию'],
    },
    placements: [{
      surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'artifact_open' }],
    }],
  },
  // The open artifact's header folder chip: shows where it is filed and opens
  // the folder picker that moves it (dragging a card onto a folder in the
  // library does the same). Its name reads "Move to folder" until it is
  // filed, then names the folder too.
  'artifacts.detail.move-to-folder': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label', key: 'pages.artifactDetailPage.move_to_folder' },
    terms: {
      en: ['move an artifact to a folder', 'put an artifact in a folder', 'file an artifact', 'organize artifacts into folders'],
      'zh-CN': ['把产物移到文件夹', '产物放进文件夹', '移动产物', '整理产物到文件夹'],
    },
    placements: [{
      surface: 'artifacts', parent: 'page.artifacts', entry: 'header',
      requires: [{ kind: 'condition', id: 'artifact_open' }],
    }],
  },
  // The open artifact's More menu: the toolbar's overflow, holding Snapshot,
  // reading width, the comments panel, pop-out and the rest.
  'artifacts.detail.more': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label', key: 'pages.artifactDetailPage.more_actions' },
    placements: [{
      surface: 'artifacts', parent: 'page.artifacts', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'artifact_open' }],
    }],
  },
  // The More menu's comments item (it reads Show / Hide comments): opens or
  // closes the comments panel.
  'artifacts.detail.comments': {
    kind: 'menu-item',
    label: { from: 'text', key: 'pages.artifactDetailPage.show_comments' },
    aliasKeys: ['pages.artifactDetailPage.hide_comments'],
    terms: {
      en: [
        'comment on an artifact', 'leave feedback on a document', 'add a comment', 'artifact comments',
        'comments on a document', 'review a document',
      ],
      'zh-CN': ['写评论', '给文档写评论', '评论文档', '添加评论', '产物评论', '文档评论', '给作品提意见'],
      ja: ['コメントを追加', 'ドキュメントにコメント'],
      ko: ['댓글 달기', '문서에 댓글'],
      es: ['añadir un comentario', 'comentar un documento'],
      fr: ['ajouter un commentaire', 'commenter un document'],
      de: ['Kommentar hinzufügen', 'Dokument kommentieren'],
      pt: ['adicionar um comentário', 'comentar um documento'],
      it: ['aggiungere un commento', 'commentare un documento'],
      ru: ['добавить комментарий', 'прокомментировать документ'],
    },
    placements: [{ surface: 'artifacts', parent: 'artifacts.detail.more', entry: 'menu' }],
  },
} as const satisfies UiLocationArea
