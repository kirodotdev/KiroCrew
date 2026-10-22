"""Knowledge Library API handlers."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the Knowledge Library routes: every name is reached through
# it as ``kiro_crew.dashboard.handlers.knowledge.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and it
# keeps no code of its own. New work goes to the owner of its responsibility:
#   ``knowledge_access`` -- the owner gate, the SEL audit, the store, pipeline and task handles
#   ``knowledge_library`` -- namespaces, items, entities, the graph, counts and statistics
#   ``knowledge_sync`` -- a source's sync status, the sync claim and the sync tasks
#   ``knowledge_sources`` -- registering and managing sources, the folder picker, file rows
#   ``knowledge_ingest`` -- agent-submitted text, uploaded files and archives, the job row
#   ``knowledge_bundles`` -- ``.knowledge`` export and import
#   ``knowledge_embeddings`` -- embedding status, rebuild and batch embed
#   ``knowledge_agent_context`` -- the chat context search and the agent's document add
#   ``knowledge_lifecycle`` -- the route table, watchers, config reloads and shutdown
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.knowledge_access",
    "kiro_crew.dashboard.handlers.knowledge_library",
    "kiro_crew.dashboard.handlers.knowledge_sync",
    "kiro_crew.dashboard.handlers.knowledge_sources",
    "kiro_crew.dashboard.handlers.knowledge_ingest",
    "kiro_crew.dashboard.handlers.knowledge_bundles",
    "kiro_crew.dashboard.handlers.knowledge_embeddings",
    "kiro_crew.dashboard.handlers.knowledge_agent_context",
    "kiro_crew.dashboard.handlers.knowledge_lifecycle",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.knowledge_access import _audited_write  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_access import _pipeline  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_access import _require_knowledge_owner  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_access import _sel_log  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_access import _store  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_access import _task_registry  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_access import _track_scan_task  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_agent_context import _estimate_tokens  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_agent_context import (  # noqa: F401
        KNOWLEDGE_FETCH_MAX_TOKENS,
        KNOWLEDGE_FETCH_TOP_N,
        _build_context_card,
        _read_knowledge_config,
        add_agent_document_route,
        search_for_context,
    )
    from kiro_crew.dashboard.handlers.knowledge_bundles import _BUNDLE_LIST_FIELDS  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_bundles import _BUNDLE_REDACTED_FIELDS  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_bundles import export_all  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_bundles import export_item  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_bundles import import_bundle  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_bundles import (  # noqa: F401
        _BUNDLE_STATE_PLAIN_FIELDS,
        _WITHHELD_REDACTED_FIELDS,
        _validate_knowledge_bundle,
    )
    from kiro_crew.dashboard.handlers.knowledge_embeddings import _active_rebuild_job  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import _embedding_counts  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import _finalize_job  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import _unembedded_count  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import _unembedded_rows  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import _write_embedding  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import batch_embed_items  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import get_embedding_status  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_embeddings import (  # noqa: F401
        _finalize_job_audited,
        _rebuild_embeddings_job,
    )
    from kiro_crew.dashboard.handlers.knowledge_ingest import _inspect_zip_archive  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_ingest import get_job  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_ingest import ingest_file  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_ingest import ingest_text  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_ingest import (  # noqa: F401
        _MAX_INGEST_ARCHIVE_MEMBERS,
        _MAX_INGEST_ARCHIVE_UNCOMPRESSED,
    )
    from kiro_crew.dashboard.handlers.knowledge_library import _NO_SOURCE  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _SCOPED_SEARCH_MAX  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _SCOPED_SEARCH_START  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _SQLITE_VARIABLE_CHUNK  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _attach_file_paths  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _entity_items_rows  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _entity_list_rows  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _item_detail  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _items_page  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _load_items_by_id  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _matches_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _namespace_rows  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _related_items  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _search_until_exhausted  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import _stats_counts  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import delete_item  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_entity_graph  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_entity_items  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_full_graph  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_item  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_item_content  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_related_items  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import get_stats  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import list_entities  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import list_items  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import list_namespaces  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import source_counts  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_library import update_item  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_lifecycle import _create_embedder  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_lifecycle import _start_watcher_async  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_lifecycle import _stop_artifact_ingest  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_lifecycle import logger  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_lifecycle import (  # noqa: F401
        _apply_knowledge_config,
        _shutdown_knowledge_pools,
        _start_artifact_ingest_async,
        _watch_knowledge_config,
        setup_knowledge_routes,
    )
    from kiro_crew.dashboard.handlers.knowledge_sources import _FOLDER_DIALOG_TIMEOUT  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _MAX_SOURCE_NAME_LEN  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _REMOTE_URI_PREFIXES  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _add_source_path_checks  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _adopt_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _create_source_audited  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _file_path_restricted  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _folder_file_rows  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _LocalUriProbe  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _pause_source_row  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _probe_local_uri  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _run_folder_dialog  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _set_file_state  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import _source_rows  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import add_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import confirm_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import delete_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import get_config  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import list_source_files  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import list_sources  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import pause_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import pick_folder  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import rename_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import resume_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import retry_file  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import skip_file  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sources import (  # noqa: F401
        _folder_picker_available,
    )
    from kiro_crew.dashboard.handlers.knowledge_sync import _add_source_unique  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _background_agent_sync  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _claim_sync  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _claim_sync_for_task  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _finalize_sync_status  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _hand_off_under_gate  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _ingest_local_file_task  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _is_syncable  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _resolved_is_sensitive  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _set_sync_status  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _source_row  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _sync_artifact_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _sync_source_body  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import _write_status_cancel_safe  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import sync_source  # noqa: F401
    from kiro_crew.dashboard.handlers.knowledge_sync import (  # noqa: F401
        _finalize_sync_status_write,
        _reconcile_artifact_source_task,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import platform_compat  # noqa: F401
    from kiro_crew._sqlite_compat import fts5_segment_for_index  # noqa: F401
    from kiro_crew._sqlite_compat import sqlite3  # noqa: F401
    from kiro_crew.artifacts import get_default_store  # noqa: F401
    from kiro_crew.config import live  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.config.loader import coerce_config_field  # noqa: F401
    from kiro_crew.config.loader import config_dir  # noqa: F401
    from kiro_crew.config.loader import data_home  # noqa: F401
    from kiro_crew.config.loader import read_config_text  # noqa: F401
    from kiro_crew.dashboard import part_stream  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import read_bounded_json  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
        require_owner_for_dashboard_caller,
    )
    from kiro_crew.dashboard.handlers.files import _ZIP_CONTAINER_EXTS  # noqa: F401
    from kiro_crew.dashboard.handlers.files import _content_matches_ext  # noqa: F401
    from kiro_crew.dashboard.handlers.files import _PathProbeBusy  # noqa: F401
    from kiro_crew.dashboard.handlers.files import _probe_busy_response  # noqa: F401
    from kiro_crew.dashboard.handlers.files import _run_path_probe  # noqa: F401
    from kiro_crew.dashboard.origin import is_direct_local_request  # noqa: F401
    from kiro_crew.dashboard.upload_limits import bytes_to_mb_figure  # noqa: F401
    from kiro_crew.dashboard.upload_limits import knowledge_upload_max_bytes  # noqa: F401
    from kiro_crew.executors import run_in_embed_pool  # noqa: F401
    from kiro_crew.knowledge.agent_fetch import fetch_url_content  # noqa: F401
    from kiro_crew.knowledge.agent_source import AGENT_SOURCE_URI  # noqa: F401
    from kiro_crew.knowledge.agent_source import add_agent_document  # noqa: F401
    from kiro_crew.knowledge.artifact_ingest import ARTIFACT_SOURCE_URI  # noqa: F401
    from kiro_crew.knowledge.artifact_ingest import ArtifactKnowledgeSync  # noqa: F401
    from kiro_crew.knowledge.chunker import HeadingAwareChunker  # noqa: F401
    from kiro_crew.knowledge.connectors.base import BaseConnector  # noqa: F401
    from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector  # noqa: F401
    from kiro_crew.knowledge.embedder import create_embedder_from_config  # noqa: F401
    from kiro_crew.knowledge.embedder import embedder_signature  # noqa: F401
    from kiro_crew.knowledge.embedder import floats_to_bytes  # noqa: F401
    from kiro_crew.knowledge.extractor import EntityExtractor  # noqa: F401
    from kiro_crew.knowledge.folder_watcher import estimate_scan_cost  # noqa: F401
    from kiro_crew.knowledge.folder_watcher import folder_chunk_budget  # noqa: F401
    from kiro_crew.knowledge.folder_watcher import max_files_prop  # noqa: F401
    from kiro_crew.knowledge.folder_watcher import walk_filters  # noqa: F401
    from kiro_crew.knowledge.ingestion import ImportChunkBudgetError  # noqa: F401
    from kiro_crew.knowledge.ingestion import IngestionPipeline  # noqa: F401
    from kiro_crew.knowledge.ingestion import _redact  # noqa: F401
    from kiro_crew.knowledge.ingestion import handoff_gate_context  # noqa: F401
    from kiro_crew.knowledge.ingestion import rebuild_embeddings  # noqa: F401
    from kiro_crew.knowledge.ingestion import start_rebuild_job  # noqa: F401
    from kiro_crew.knowledge.llm_pool import DEFAULT_EXTRACTION_EFFORT  # noqa: F401
    from kiro_crew.knowledge.llm_pool import LLMPool  # noqa: F401
    from kiro_crew.knowledge.readers import FileReader  # noqa: F401
    from kiro_crew.knowledge.retrieval import HybridRetriever  # noqa: F401
    from kiro_crew.knowledge.retrieval import vector_leg  # noqa: F401
    from kiro_crew.knowledge.spend import source_spend  # noqa: F401
    from kiro_crew.knowledge.store import AUTO_REGISTRATION_RETIRED_PROP  # noqa: F401
    from kiro_crew.knowledge.store import BUNDLE_STATE_KEY_COL  # noqa: F401
    from kiro_crew.knowledge.store import KnowledgeBundleError  # noqa: F401
    from kiro_crew.knowledge.sync import SyncScheduler  # noqa: F401
    from kiro_crew.knowledge.watcher import KnowledgeWatcher  # noqa: F401
    from kiro_crew.security import is_sensitive_path  # noqa: F401
    from kiro_crew.sel import sel  # noqa: F401
    from kiro_crew.validation import clamp_query_offset  # noqa: F401
    from kiro_crew.zip_vet import ZipInventoryRejected  # noqa: F401
    from kiro_crew.zip_vet import vet_zip_inventory  # noqa: F401
