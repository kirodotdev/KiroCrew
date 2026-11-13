"""The Files backend and the core file-read endpoint agree about what "binary" means.

``dashboard/handlers/files.py`` answers "is this binary" for the chat side panel,
and this app's ``server.BINARY_EXTS`` answers it for the Files page. The two
surfaces disagreeing is the worst outcome either list could have, so the app pins
its copy against the core's here. The app reads the core; the core never reads
the app.
"""

from kiro_crew.apps.builtins.file_explorer.server import BINARY_EXTS
from kiro_crew.dashboard.handlers.files import _FILE_READ_BINARY_EXTS


def test_binary_extension_list_matches_the_core_file_read_endpoint():
    assert set(BINARY_EXTS) == set(_FILE_READ_BINARY_EXTS)
