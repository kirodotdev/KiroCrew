"""PPTX Maker's data-URI credential scan reads its spelling from the core's one home.

``backend/routes.py`` refuses a data URI whose base64 body carries an AWS key id,
and it must recognise exactly the ids the core's redaction and scrubbing do, so it
compiles ``kiro_crew.credential_patterns.AWS_KEY_ID`` instead of a copy. The core's
own sites are pinned in ``test/test_credential_patterns.py``. The app reads the
core; the core never reads the app.
"""

from kiro_crew import credential_patterns
from kiro_crew.apps.builtins.pptx_maker.backend import routes


def test_the_data_uri_scan_compiles_the_shared_aws_key_id_spelling():
    assert routes._ENCODED_CREDENTIAL_RE.pattern == credential_patterns.AWS_KEY_ID
