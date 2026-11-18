"""The rule table as data, and the loader that validates it.

A row names its id, tier, detector and the data that detector reads: prefixes,
alphabets, lengths, checksum ids, key names, armour lines. There is no pattern
field; the loader refuses any field a detector does not read, ``pattern`` included.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .charsets import ALPHABETS, BOUNDARIES
from .model import IDENTIFIER, SECRET, TIERS


class TableError(ValueError):
    """A row the loader refuses."""


# ── Rows ──────────────────────────────────────────────────────────────────────

#: The JWT examples published on jwt.io (the current default and the two older
#: samples) and in RFC 7519 section 3.1: documentation, signed with a published key, so
#: they grant nothing. The ``jwt`` row and the Bearer header row both allow them.
JOSE_EXAMPLES = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWV9"
    ".TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWUsImlhdCI6MTUxNjIzOTAyMn0"
    ".KMUFsIDTnFmyG3nMiGM6H9FNFUROf3wh7SmqJp-QV30",
    "eyJ0eXAiOiJKV1QiLA0KICJhbGciOiJIUzI1NiJ9"
    ".eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
    ".dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
)


ROWS: tuple[Mapping[str, Any], ...] = (
    # Detector B: whole tokens by published format.
    {
        "id": "github.classic",
        "tier": SECRET,
        "detector": "token_format",
        "certainty": "checksum",
        "prefixes": ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"],
        "body": [{"alphabet": "base62", "length": 36}],
        "boundary": "word",
        "checksum": "github_crc32",
    },
    {
        "id": "github.fine_grained",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["github_pat_"],
        "body": [
            {"alphabet": "base62", "length": 22},
            {"literal": ["_"]},
            {"alphabet": "base62", "length": 59},
        ],
        "boundary": "word",
    },
    {
        "id": "slack.token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-"],
        "body": [{"alphabet": "slack", "min": 10, "max": 250}],
        "boundary": "word_dash",
    },
    {
        "id": "slack.app_token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["xapp-"],
        "body": [{"alphabet": "slack", "min": 10, "max": 250}],
        "boundary": "word_dash",
    },
    {
        "id": "huggingface.token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["hf_"],
        "body": [{"alphabet": "base62", "length": 34}],
        "boundary": "word",
    },
    {
        "id": "docker.pat",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["dckr_pat_"],
        "body": [{"alphabet": "b64url", "length": 27}],
        "boundary": "word_dash",
    },
    {
        # The older OpenAI key: 20 characters, a fixed marker, 20 characters.
        "id": "openai.legacy_key",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["sk-"],
        "body": [
            {"alphabet": "base62", "length": 20},
            {"literal": ["T3Blbk" + "FJ"]},
            {"alphabet": "base62", "length": 20},
        ],
        "boundary": "word_dash",
    },
    {
        "id": "stripe.key",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["sk_live_", "sk_test_", "rk_live_", "rk_test_"],
        "body": [{"alphabet": "base62", "min": 16, "max": 247}],
        "boundary": "word",
    },
    {
        # Length from a third-party description (32 or 64 characters), not yet from
        # Stripe's own documentation: design open question M8.
        "id": "stripe.webhook_secret",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["whsec_"],
        "body": [{"alphabet": "base62", "lengths": [32, 64]}],
        "boundary": "word",
    },
    {
        "id": "anthropic.key",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["sk-ant-"],
        "body": [
            {"literal": ["admin", "api"]},
            {"alphabet": "digits", "length": 2},
            {"literal": ["-"]},
            {"alphabet": "b64url", "min": 80, "max": 200},
        ],
        "boundary": "word_dash",
    },
    {
        "id": "google.api_key",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["AIza"],
        "body": [{"alphabet": "b64url", "length": 35}],
        "boundary": "word_dash",
    },
    {
        "id": "google.oauth_client_secret",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["GOCSPX-"],
        "body": [{"alphabet": "b64url", "min": 20, "max": 64}],
        "boundary": "word_dash",
    },
    {
        "id": "gitlab.pat",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["glpat-"],
        "body": [{"alphabet": "b64url", "min": 20, "max": 64}],
        "boundary": "word_dash",
    },
    {
        "id": "sendgrid.key",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["SG."],
        "body": [
            {"alphabet": "b64url", "length": 22},
            {"literal": ["."]},
            {"alphabet": "b64url", "length": 43},
        ],
        "left_boundary": "word_dash_dot",
        "right_boundary": "word_dash",
    },
    {
        "id": "hashicorp.vault",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["hvs.", "hvb.", "hvr."],
        "body": [{"alphabet": "b64url", "min": 24, "max": 250}],
        "left_boundary": "word_dash_dot",
        "right_boundary": "word_dash",
    },
    {
        "id": "shopify.token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["shpat_", "shpca_", "shppa_", "shpss_"],
        "body": [{"alphabet": "hex_any", "length": 32}],
        "boundary": "word",
    },
    {
        "id": "digitalocean.token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["dop_v1_", "doo_v1_", "dor_v1_"],
        "body": [{"alphabet": "hex_lower", "length": 64}],
        "boundary": "word",
    },
    {
        "id": "openai.project_key",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["sk-proj-"],
        "body": [{"alphabet": "b64url", "min": 16, "max": 256}],
        # Read after a ``-`` too (a CSS custom property, ``--sk-proj-...``).
        "left_boundary": "word",
        "right_boundary": "word_dash",
    },
    {
        "id": "npm.token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["npm_"],
        "body": [{"alphabet": "base62", "min": 24, "max": 64}],
        "boundary": "word",
    },
    {
        "id": "pypi.token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["pypi-"],
        "body": [{"alphabet": "b64url", "min": 16, "max": 1024}],
        "boundary": "word_dash",
    },
    {
        # A Discord bot token: base64 of the bot's id, whose first character is ``M``,
        # ``N`` or ``O`` for every id in use, a 6-character timestamp and an HMAC.
        "id": "discord.bot_token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["M", "N", "O"],
        "body": [
            {"alphabet": "b64url", "min": 22, "max": 30},
            {"literal": ["."]},
            {"alphabet": "b64url", "length": 6},
            {"literal": ["."]},
            {"alphabet": "b64url", "min": 25, "max": 100},
        ],
        "boundary": "b64url",
    },
    {
        # A Telegram bot token as Telegram issues it: an 8 to 10 digit bot id (its first
        # digit is the prefix), ``:`` and 35 characters. Read after ``bot`` too, as the
        # Bot API URL writes it, but not after the ``:`` or ``/`` of an ARN's fields.
        "id": "telegram.bot_token",
        "tier": SECRET,
        "detector": "token_format",
        "prefixes": ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
        "body": [
            {"alphabet": "digits", "min": 7, "max": 9},
            {"literal": [":"]},
            {"alphabet": "b64url", "length": 35},
        ],
        "left_boundary": "telegram_left",
        "right_boundary": "b64url",
    },
    # Identifier-tier tokens: never replaced; the key ID anchors the pairing rule.
    {
        "id": "aws.access_key_id",
        "tier": IDENTIFIER,
        "detector": "token_format",
        "prefixes": ["AKIA", "ASIA", "AROA", "AIDA"],
        "body": [{"alphabet": "upper_digit", "length": 16}],
        "left_boundary": "aws_key_id_left",
        "right_boundary": "aws_key_id_right",
    },
    {
        "id": "twilio.account_sid",
        "tier": IDENTIFIER,
        "detector": "token_format",
        "prefixes": ["AC"],
        "body": [{"alphabet": "hex_lower", "length": 32}],
        "boundary": "word",
    },
    # JOSE tokens: segment counts, per-segment length bounds, decoded structure.
    {
        "id": "jwt",
        "tier": SECRET,
        "detector": "jose",
        "segments": 3,
        "segment_lengths": [[4, 2000], [4, 12000], [1, 2000]],
        "eyj_segments": [0, 1],
        "header_keys": ["alg"],
        "payload_object": True,
        # A token may follow a sentence's ``.`` (``Done.eyJ...``); the decoded header
        # check, not the character before it, tells it from a property access.
        "left_boundary": "b64url",
        # The published examples (JOSE_EXAMPLES). Read by header and payload, so a
        # reading that drops markup from the signature is the same example.
        "allow_values": list(JOSE_EXAMPLES),
    },
    {
        "id": "jwe",
        "tier": SECRET,
        "detector": "jose",
        "segments": 5,
        # JOSE compact form only: 5 segments, base64url, a JSON header naming alg and
        # enc. The encrypted key, IV, ciphertext and tag may each be empty; the upper
        # bounds only cap a stream's hold.
        "segment_lengths": [[4, 2000], [0, 2000], [0, 100], [0, 5000], [0, 100]],
        "eyj_segments": [0],
        "header_keys": ["alg", "enc"],
        "payload_object": False,
        "left_boundary": "b64url",
    },
    {
        # Crew's own dashboard link token: base64url(JSON claims).HMAC-SHA256.
        "id": "link_token",
        "tier": SECRET,
        "detector": "jose",
        "segments": 2,
        "segment_lengths": [[96, 500], [43, 43]],
        "eyj_segments": [0],
        "payload_claims": ["exp", "iat", "nonce", "sub"],
        "left_boundary": "b64url",
        "right_boundary": "b64url",
    },
    # Detector B pairing: a 40-character AWS secret beside a key ID.
    {
        "id": "aws.secret_access_key",
        "tier": SECRET,
        "detector": "key_id_pair",
        "alphabet": "b64std",
        "length": 40,
        "left_boundary": "b64std",
        "right_boundary": "b64std_pad",
        "pair_prefixes": ["AKIA", "ASIA"],
        "window": 300,
        "allow_suffixes": ["EXAMPLEKEY"],
    },
    # Detector C: private-key armour. ``begin``/``end`` are split so this file does
    # not itself hold a whole armour line.
    {
        "id": "pem.private_key",
        "unterminated_id": "pem.private_key_unterminated",
        "tier": SECRET,
        "detector": "armour",
        "begin": "-----" + "BEGIN ",
        "type_alphabet": "upper_space",
        "type_max": 60,
        "suffix": "PRIVATE " + "KEY-----",
        "end": "-----" + "END ",
        "max_block": 16000,
    },
    {
        "id": "pgp.private_key",
        "unterminated_id": "pgp.private_key_unterminated",
        "tier": SECRET,
        "detector": "armour",
        "begin": "-----" + "BEGIN ",
        "type_max": 0,
        "suffix": "PGP PRIVATE " + "KEY BLOCK-----",
        "end": "-----" + "END ",
        "max_block": 16000,
    },
    # Detector A: the four AWS names, with the value-shape gate (owner decision O1b).
    {
        "id": "aws.secret_access_key.label",
        "tier": SECRET,
        "detector": "known_key",
        "keys": ["aws_secret_access_key", "SecretAccessKey"],
        "inputs": ["json", "assignment", "cli_flag", "xml_element"],
        "gate": "aws_secret_40",
    },
    {
        "id": "aws.session_token.label",
        "tier": SECRET,
        "detector": "known_key",
        "keys": ["aws_session_token", "SessionToken"],
        "inputs": ["json", "assignment", "cli_flag", "xml_element"],
        "gate": "b64_run",
    },
    # Detector A, a written credential after a well-known credential name: an env file, a
    # JSON or YAML config, a CLI flag, an XML element. The name is one of ``keys``, compared
    # case-insensitively with ``_``, ``-``, ``.`` and blanks dropped (``DB_PASSWORD``,
    # ``dbPassword`` and ``--db-password`` are one name); a name the list does not hold is
    # not read (the known gaps: ``test_redaction_structured_names``). Only the value is
    # replaced; a placeholder, a code expression or words after the name stay (gate
    # ``secret_literal``).
    {
        "id": "labelled.secret",
        "tier": SECRET,
        "detector": "known_key",
        "keys": [
            # Generic names.
            "password",
            "passwd",
            "secret",
            "token",
            "api_key",
            "apikey",
            "api_secret",
            "api_secret_key",
            "secret_key",
            "client_secret",
            "app_secret",
            "jwt_secret",
            "auth_token",
            "access_token",
            "refresh_token",
            "private_token",
            # Databases and mail.
            "db_password",
            "database_password",
            "postgres_password",
            "pgpassword",
            "mysql_password",
            "mysql_pwd",
            "mysql_root_password",
            "redis_password",
            "admin_password",
            "root_password",
            "smtp_password",
            "spring.datasource.password",
            # Services.
            "github_token",
            "gh_token",
            "npm_token",
            "slack_bot_token",
            "openai_api_key",
            "anthropic_api_key",
            "twilio_auth_token",
            "sentry_auth_token",
            # AWS ECR ``get-authorization-token`` output (``authorizationToken``).
            "authorization_token",
            # Azure storage connection-string fields.
            "account_key",
            "shared_access_key",
        ],
        "inputs": ["json", "assignment", "cli_flag", "xml_element"],
        "gate": "secret_literal",
    },
    # Detector A on parsed URLs: the owner's exact OAuth keys in a URL's query string
    # or fragment. Only the value is replaced; the key and the rest of the URL stay.
    {"id": "url.oauth_param.client_secret", "tier": SECRET, "detector": "url_param",
     "key": "client_secret"},  # fmt: skip
    {"id": "url.oauth_param.access_token", "tier": SECRET, "detector": "url_param",
     "key": "access_token"},  # fmt: skip
    {"id": "url.oauth_param.refresh_token", "tier": SECRET, "detector": "url_param",
     "key": "refresh_token"},  # fmt: skip
    {"id": "url.oauth_param.id_token", "tier": SECRET, "detector": "url_param",
     "key": "id_token"},  # fmt: skip
    {"id": "url.oauth_param.code_verifier", "tier": SECRET, "detector": "url_param",
     "key": "code_verifier"},  # fmt: skip
    {"id": "url.oauth_param.password", "tier": SECRET, "detector": "url_param",
     "key": "password"},  # fmt: skip
    # Values that grant access by themselves wherever a URL carries them: a password, an
    # API key, and the signature or session token of a signed (presigned, SAS) link.
    {"id": "url.secret_param.passwd", "tier": SECRET, "detector": "url_param",
     "key": "passwd"},  # fmt: skip
    {"id": "url.secret_param.api_key", "tier": SECRET, "detector": "url_param",
     "key": "api_key"},  # fmt: skip
    {"id": "url.secret_param.apikey", "tier": SECRET, "detector": "url_param",
     "key": "apikey"},  # fmt: skip
    {"id": "url.secret_param.private_token", "tier": SECRET, "detector": "url_param",
     "key": "private_token"},  # fmt: skip
    {"id": "url.signature_param.sig", "tier": SECRET, "detector": "url_param",
     "key": "sig"},  # fmt: skip
    {"id": "url.signature_param.signature", "tier": SECRET, "detector": "url_param",
     "key": "signature"},  # fmt: skip
    {"id": "url.signature_param.x_amz_signature", "tier": SECRET, "detector": "url_param",
     "key": "x-amz-signature"},  # fmt: skip
    {"id": "url.signature_param.x_amz_security_token", "tier": SECRET, "detector": "url_param",
     "key": "x-amz-security-token"},  # fmt: skip
    {"id": "url.signature_param.x_goog_signature", "tier": SECRET, "detector": "url_param",
     "key": "x-goog-signature"},  # fmt: skip
    # A URL's ``token`` parameter: the bearer value of a link (a dashboard, a webhook, a
    # signed download). An owner's exempt companion URL keeps its value
    # (``redaction._exempt_token_values``).
    {"id": "url.token_param.token", "tier": SECRET, "detector": "url_param",
     "key": "token"},  # fmt: skip
    # Detector H: HTTP header values, session cookies and the curl user password.
    {
        "id": "http.bearer_token",
        "tier": SECRET,
        "detector": "header",
        "reader": "header",
        "names": ["authorization"],
        "scheme": "bearer",
        "min_length": 8,
        # A published JWT example after ``Bearer`` is documentation too.
        "allow_values": list(JOSE_EXAMPLES),
    },
    {
        # ``Bearer <token>`` with no ``Authorization`` name before it (a logged request line,
        # a pasted curl ``-H`` value, a prose sentence). Words ("Bearer of bad news") stay.
        "id": "http.bearer_bare",
        "tier": SECRET,
        "detector": "header",
        "reader": "bearer",
        "min_length": 8,
        "allow_values": list(JOSE_EXAMPLES),
    },
    {
        "id": "http.basic_credentials",
        "tier": SECRET,
        "detector": "header",
        "reader": "header",
        "names": ["authorization"],
        "scheme": "basic",
        "min_length": 8,
    },
    {
        "id": "http.api_key_header",
        "tier": SECRET,
        "detector": "header",
        "reader": "header",
        "names": ["x-api-key", "api-key", "x-auth-token", "private-token", "x-goog-api-key"],
        "min_length": 8,
    },
    {
        "id": "http.session_cookie",
        "tier": SECRET,
        "detector": "header",
        "reader": "cookie",
        "names": ["set-cookie", "cookie"],
        "min_length": 8,
    },
    {
        "id": "cli.curl_user_password",
        "tier": SECRET,
        "detector": "header",
        "reader": "curl_user",
        "min_length": 4,
    },
    {
        # ``mysql -u root -p<password>`` (the password glued to ``-p``) and
        # ``--password=<password>``, on a line where one of ``names`` is a command word.
        "id": "cli.mysql_password",
        "tier": SECRET,
        "detector": "header",
        "reader": "mysql_password",
        "names": ["mysql", "mysqldump", "mysqladmin", "mariadb"],
        "min_length": 4,
    },
    {
        # ``docker login -u <user> -p <password>`` (also ``--password``), on a line where
        # one of ``names`` is a command word, with its ``login`` subcommand.
        "id": "cli.docker_login_password",
        "tier": SECRET,
        "detector": "header",
        "reader": "docker_login",
        "names": ["docker"],
        "min_length": 4,
    },
    {
        # A ``.netrc`` entry: ``machine <host> login <user> password <password>``, on one
        # line or across lines.
        "id": "netrc.password",
        "tier": SECRET,
        "detector": "header",
        "reader": "netrc",
        "min_length": 4,
    },
    # A webhook URL whose path carries its secret: only that path segment is replaced.
    {
        "id": "slack.webhook_url",
        "tier": SECRET,
        "detector": "url_path_secret",
        "hosts": ["hooks.slack.com"],
        "path_prefix": "/services/",
        "segment": 3,
    },
    {
        "id": "discord.webhook_url",
        "tier": SECRET,
        "detector": "url_path_secret",
        "hosts": ["discord.com", "discordapp.com"],
        "path_prefix": "/api/webhooks/",
        "segment": 2,
    },
    # Detector E: a base64 run whose decoded text holds a credential another row reads.
    {"id": "encoded_credential", "tier": SECRET, "detector": "encoded", "min_length": 24},
    # The password in a URL's userinfo (``scheme://user:password@host``); the scheme,
    # the user and the host stay.
    {"id": "url.userinfo_password", "tier": SECRET, "detector": "url_userinfo"},
    # Structural Identifiers: never replaced; they protect text they fully contain
    # from label and pairing hits (resolve rule 1).
    {
        # Git's published object-name format: exactly 40 (SHA-1) or 64 (SHA-256)
        # characters of lower-case hex, as a whole token (no letter or digit on
        # either side). It contains, and so overrides, a pairing or label hit on the
        # same run.
        "id": "git.object_name",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "object_name",
        "alphabet": "hex_lower",
        "lengths": [40, 64],
        "max_length": 64,
    },
    {
        "id": "arn",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "arn",
        "max_length": 2048,
    },
    {
        "id": "url.structure",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "url",
        "max_length": 4096,
    },
    {
        "id": "commit.permalink",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "commit_permalink",
        "max_length": 4096,
    },
    {
        "id": "oauth.public_param",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "url_query",
        "params": [
            "client_id",
            "code_challenge",
            "code_challenge_method",
            "nonce",
            "redirect_uri",
            "response_type",
            "scope",
            "state",
        ],
        "max_length": 4096,
    },
    # Identifier values decided by structure only (fixed labels, schemes, published
    # values and file signatures). Never replaced; never override a Secret hit.
    {
        "id": "aws.x_amz_id_2",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "labelled_value",
        "labels": ["x-amz-id-2:", '"hostid":', "<hostid>"],
        "alphabet": "b64std",
        "min_length": 20,
        "max_length": 200,
        "pad": 2,
    },
    {
        "id": "aws.request_id",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "labelled_value",
        "labels": ["x-amz-request-id:", "x-amzn-requestid:", '"requestid":', "<requestid>"],
        "alphabet": "hex_any_dash",
        "min_length": 16,
        "max_length": 36,
        "pad": 0,
    },
    {
        # OpenSSH prints a SHA-256 key fingerprint as SHA256: plus 43 unpadded base64.
        "id": "ssh.fingerprint",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "labelled_value",
        "labels": ["sha256:"],
        "alphabet": "b64std",
        "min_length": 43,
        "max_length": 43,
        "pad": 0,
    },
    {
        "id": "data_uri",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "data_uri",
        "max_length": 65536,
    },
    {
        # Base64 of the PNG, JPEG, GIF and RIFF (WebP) file signatures.
        "id": "image.base64",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "image_b64",
        "labels": ["iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR"],
        "min_length": 40,
        "max_length": 65536,
    },
    {
        # The example key ID and secret published in the AWS documentation.
        "id": "aws.doc_example",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "exact",
        "values": ["AKIAIOSFODNN7" + "EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"],
        "max_length": 64,
    },
    {
        "id": "macos.temp_path",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "temp_path",
        "max_length": 1024,
    },
    {
        "id": "placeholder",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "placeholder",
        "max_length": 128,
    },
    {
        "id": "oauth.form_param",
        "tier": IDENTIFIER,
        "detector": "structure",
        "kind": "form_query",
        "params": [
            "client_id",
            "code_challenge",
            "code_challenge_method",
            "nonce",
            "redirect_uri",
            "response_type",
            "scope",
            "state",
        ],
        "max_length": 4096,
    },
)


# ── Loaded forms ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Part:
    literals: tuple[str, ...] = ()
    chars: str = ""
    min: int = 0
    max: int = 0
    lengths: frozenset[int] | None = None

    @property
    def is_literal(self) -> bool:
        return bool(self.literals)

    @property
    def max_len(self) -> int:
        return max(len(x) for x in self.literals) if self.literals else self.max


@dataclass(frozen=True, slots=True)
class FormatRow:
    id: str
    tier: str
    certainty: str
    prefixes: tuple[str, ...]
    parts: tuple[Part, ...]
    left: frozenset[str]
    right: frozenset[str]
    run_chars: str
    max_total: int
    checksum: str | None


@dataclass(frozen=True, slots=True)
class JoseRow:
    id: str
    tier: str
    segments: int
    segment_lengths: tuple[tuple[int, int], ...]
    eyj_segments: frozenset[int]
    header_keys: tuple[str, ...]
    payload_object: bool
    payload_claims: frozenset[str]
    left: frozenset[str]
    right: frozenset[str]
    # Header and payload of the documented examples (``allow_values`` without the signature).
    allow_values: frozenset[str] = frozenset()

    @property
    def max_total(self) -> int:
        return (
            sum(hi for _, hi in self.segment_lengths)
            + 3 * len(self.eyj_segments)
            + (self.segments - 1)
        )


@dataclass(frozen=True, slots=True)
class PairRow:
    id: str
    tier: str
    chars: str
    length: int
    left: frozenset[str]
    right: frozenset[str]
    pair_prefixes: tuple[str, ...]
    window: int
    allow_suffixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArmourRow:
    id: str
    unterminated_id: str
    tier: str
    begin: str
    type_chars: str
    type_max: int
    suffix: str
    end: str
    max_block: int


@dataclass(frozen=True, slots=True)
class KnownKeyRow:
    id: str
    tier: str
    keys: tuple[str, ...]
    inputs: frozenset[str]
    gate: str


@dataclass(frozen=True, slots=True)
class StructureRow:
    id: str
    tier: str
    kind: str
    params: frozenset[str]
    max_length: int
    labels: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    chars: str = ""
    min_length: int = 0
    pad: int = 0
    lengths: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class UrlParamRow:
    id: str
    tier: str
    key: str


@dataclass(frozen=True, slots=True)
class UrlUserinfoRow:
    id: str
    tier: str


@dataclass(frozen=True, slots=True)
class HeaderRow:
    id: str
    tier: str
    reader: str
    names: tuple[str, ...]
    scheme: str
    min_length: int
    # Header and payload of the JOSE examples a value may be (``allow_values`` without
    # the signature).
    allow_values: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class UrlPathSecretRow:
    id: str
    tier: str
    hosts: frozenset[str]
    path_prefix: str
    segment: int


@dataclass(frozen=True, slots=True)
class EncodedRow:
    id: str
    tier: str
    min_length: int


@dataclass(frozen=True, slots=True)
class Table:
    formats: tuple[FormatRow, ...]
    jose: tuple[JoseRow, ...]
    pairs: tuple[PairRow, ...]
    armour: tuple[ArmourRow, ...]
    known_keys: tuple[KnownKeyRow, ...]
    structures: tuple[StructureRow, ...]
    url_params: tuple[UrlParamRow, ...] = ()
    userinfo: tuple[UrlUserinfoRow, ...] = ()
    encoded: tuple[EncodedRow, ...] = ()
    path_secrets: tuple[UrlPathSecretRow, ...] = ()
    headers: tuple[HeaderRow, ...] = ()

    def rule_ids(self) -> list[tuple[str, str, str]]:
        """``(rule_id, detector, tier)`` for every rule the table can emit."""
        out: list[tuple[str, str, str]] = []
        out += [(r.id, "token_format", r.tier) for r in self.formats]
        out += [(r.id, "jose", r.tier) for r in self.jose]
        out += [(r.id, "key_id_pair", r.tier) for r in self.pairs]
        for a in self.armour:
            out += [(a.id, "armour", a.tier), (a.unterminated_id, "armour", a.tier)]
        out += [(r.id, "known_key", r.tier) for r in self.known_keys]
        out += [(r.id, "url_param", r.tier) for r in self.url_params]
        out += [(r.id, "url_userinfo", r.tier) for r in self.userinfo]
        out += [(r.id, "encoded", r.tier) for r in self.encoded]
        out += [(r.id, "url_path_secret", r.tier) for r in self.path_secrets]
        out += [(r.id, "header", r.tier) for r in self.headers]
        out += [(r.id, "structure", r.tier) for r in self.structures]
        return out


# ── Loader ────────────────────────────────────────────────────────────────────

_COMMON = frozenset({"id", "tier", "detector"})
_FIELDS: dict[str, frozenset[str]] = {
    "token_format": _COMMON
    | {"certainty", "prefixes", "body", "boundary", "left_boundary", "right_boundary", "checksum"},
    "jose": _COMMON
    | {
        "segments",
        "segment_lengths",
        "eyj_segments",
        "header_keys",
        "payload_object",
        "payload_claims",
        "left_boundary",
        "right_boundary",
        "allow_values",
    },
    "key_id_pair": _COMMON
    | {
        "alphabet",
        "length",
        "left_boundary",
        "right_boundary",
        "pair_prefixes",
        "window",
        "allow_suffixes",
    },
    "armour": _COMMON
    | {"unterminated_id", "begin", "type_alphabet", "type_max", "suffix", "end", "max_block"},
    "known_key": _COMMON | {"keys", "inputs", "gate"},
    "url_param": _COMMON | {"key"},
    "url_userinfo": _COMMON,
    "encoded": _COMMON | {"min_length"},
    "url_path_secret": _COMMON | {"hosts", "path_prefix", "segment"},
    "header": _COMMON | {"reader", "names", "scheme", "min_length", "allow_values"},
    "structure": _COMMON
    | {
        "kind",
        "params",
        "max_length",
        "labels",
        "values",
        "alphabet",
        "min_length",
        "pad",
        "lengths",
    },
}
#: The URL query and fragment keys whose values are replaced: each value grants access
#: by itself (a secret, a token, a password, an API key, or the signature or session
#: token of a signed link). A ``url_param`` row may name only one of these. Names whose
#: value does not grant access alone stay readable and are refused here: ``code`` (an
#: OAuth authorization code is single use and needs the client secret or the PKCE
#: verifier), ``state``, ``client_id``, ``x-amz-credential`` (an access key ID and a
#: scope); ``key`` and ``secret`` are too generic to judge by name.
URL_PARAM_KEYS = frozenset(
    {
        "token",
        "client_secret",
        "access_token",
        "refresh_token",
        "id_token",
        "code_verifier",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "private_token",
        "sig",
        "signature",
        "x-amz-signature",
        "x-amz-security-token",
        "x-goog-signature",
    }
)
#: Parameters whose values are credentials (and ``code``): an Identifier parameter row
#: may never list them.
NEVER_MASK_PARAMS = URL_PARAM_KEYS | {"code"}
_PART_FIELDS = frozenset({"alphabet", "length", "min", "max", "lengths", "literal"})
_CHECKSUMS = frozenset({"github_crc32"})
_CERTAINTIES = frozenset({"checksum", "fixed_format"})
_GATES = frozenset({"aws_secret_40", "b64_run", "secret_literal"})
_INPUTS = frozenset({"json", "assignment", "cli_flag", "xml_element"})
_KINDS = frozenset({"arn", "url", "commit_permalink", "url_query", "object_name"})
#: Fields each structural kind requires (beyond id, tier, detector, kind, max_length).
_KIND_NEEDS: dict[str, frozenset[str]] = {
    "url_query": frozenset({"params"}),
    "object_name": frozenset({"alphabet", "lengths"}),
}
#: Identifier-value kinds (``identifier_values``) and the fields each one requires.
VALUE_KINDS: dict[str, frozenset[str]] = {
    "labelled_value": frozenset({"labels", "alphabet", "min_length", "pad"}),
    "data_uri": frozenset(),
    "image_b64": frozenset({"labels", "min_length"}),
    "exact": frozenset({"values"}),
    "temp_path": frozenset(),
    "placeholder": frozenset(),
    "form_query": frozenset({"params"}),
}
_VALUE_FIELDS = frozenset({"labels", "values", "alphabet", "min_length", "pad", "lengths"})
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._")


def _fail(row: Mapping[str, Any], why: str) -> TableError:
    return TableError(f"rule {row.get('id', '?')!r}: {why}")


def _alphabet(row: Mapping[str, Any], name: Any) -> str:
    if name not in ALPHABETS:
        raise _fail(row, f"unknown alphabet {name!r}")
    return ALPHABETS[name]


def _boundary(row: Mapping[str, Any], name: Any) -> frozenset[str]:
    if name not in BOUNDARIES:
        raise _fail(row, f"unknown boundary {name!r}")
    return frozenset(BOUNDARIES[name])


def _strings(row: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = row.get(field)
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise _fail(row, f"{field} must be a non-empty list")
    if not all(isinstance(v, str) and v for v in value):
        raise _fail(row, f"{field} must hold non-empty strings")
    return tuple(value)


def _int(row: Mapping[str, Any], field: str, lo: int = 0) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < lo:
        raise _fail(row, f"{field} must be an integer >= {lo}")
    return value


def _build_part(row: Mapping[str, Any], data: Any) -> Part:
    if not isinstance(data, Mapping):
        raise _fail(row, "a body part must be a mapping")
    unknown = set(data) - _PART_FIELDS
    if unknown:
        raise _fail(row, f"unknown body field(s) {sorted(unknown)}")
    if "literal" in data:
        if set(data) != {"literal"}:
            raise _fail(row, "a literal part takes no other field")
        lits = _strings({"id": row.get("id"), "literal": data["literal"]}, "literal")
        return Part(literals=tuple(sorted(lits, key=len, reverse=True)))
    chars = _alphabet(row, data.get("alphabet"))
    if "length" in data:
        n = _int(data, "length", 1)
        return Part(chars=chars, min=n, max=n)
    if "lengths" in data:
        ls = data["lengths"]
        if not isinstance(ls, list) or not ls or not all(isinstance(x, int) and x > 0 for x in ls):
            raise _fail(row, "lengths must be a list of positive integers")
        return Part(chars=chars, min=min(ls), max=max(ls), lengths=frozenset(ls))
    lo, hi = _int(data, "min", 1), _int(data, "max", 1)
    if lo > hi:
        raise _fail(row, "min must not exceed max")
    return Part(chars=chars, min=lo, max=hi)


def _build_format(row: Mapping[str, Any]) -> FormatRow:
    prefixes = _strings(row, "prefixes")
    body = row.get("body")
    if not isinstance(body, list) or not body:
        raise _fail(row, "body must be a non-empty list")
    parts = tuple(_build_part(row, p) for p in body)
    for here, after in zip(parts, parts[1:], strict=False):
        # A variable run followed by a literal must stop at that literal by itself.
        if not here.is_literal and here.min != here.max and after.is_literal:
            if any(lit[0] in here.chars for lit in after.literals):
                raise _fail(row, "a variable run must not hold the literal that ends it")
    if "boundary" in row and ("left_boundary" in row or "right_boundary" in row):
        raise _fail(row, "boundary and left_boundary/right_boundary are exclusive")
    left = _boundary(row, row.get("left_boundary", row.get("boundary")))
    right = _boundary(row, row.get("right_boundary", row.get("boundary")))
    if parts[-1].is_literal or not set(parts[-1].chars) <= right:
        raise _fail(row, "the last body part must be a run inside the right boundary")
    certainty = row.get("certainty", "fixed_format")
    if certainty not in _CERTAINTIES:
        raise _fail(row, f"unknown certainty {certainty!r}")
    checksum = row.get("checksum")
    if checksum is not None and checksum not in _CHECKSUMS:
        raise _fail(row, f"unknown checksum {checksum!r}")
    if (certainty == "checksum") != (checksum is not None):
        raise _fail(row, "certainty checksum needs a checksum, and only then")
    run = set(right)
    for p in parts:
        run |= set(p.chars)
        for lit in p.literals:
            run |= set(lit)
    for prefix in prefixes:
        run |= set(prefix)
    max_total = max(len(p) for p in prefixes) + sum(p.max_len for p in parts)
    return FormatRow(
        id=row["id"],
        tier=row["tier"],
        certainty=certainty,
        prefixes=prefixes,
        parts=parts,
        left=left,
        right=right,
        run_chars="".join(sorted(run)),
        max_total=max_total,
        checksum=checksum,
    )


def _build_jose(row: Mapping[str, Any]) -> JoseRow:
    segments = _int(row, "segments", 2)
    lengths = row.get("segment_lengths")
    if (
        not isinstance(lengths, list)
        or len(lengths) != segments
        or not all(isinstance(x, list) and len(x) == 2 and 0 <= x[0] <= x[1] for x in lengths)
    ):
        raise _fail(row, "segment_lengths must give [min, max] per segment")
    eyj = row.get("eyj_segments", [])
    if not isinstance(eyj, list) or not all(isinstance(i, int) and 0 <= i < segments for i in eyj):
        raise _fail(row, "eyj_segments must index segments")
    if 0 not in eyj:
        raise _fail(row, "the first segment is the eyJ anchor")
    header_keys = row.get("header_keys", [])
    if not isinstance(header_keys, list) or not all(isinstance(k, str) and k for k in header_keys):
        raise _fail(row, "header_keys must be a list of non-empty strings")
    claims = row.get("payload_claims", [])
    if not isinstance(claims, list) or not all(isinstance(c, str) for c in claims):
        raise _fail(row, "payload_claims must be a list of strings")
    return JoseRow(
        id=row["id"],
        tier=row["tier"],
        segments=segments,
        segment_lengths=tuple((lo, hi) for lo, hi in lengths),
        eyj_segments=frozenset(eyj),
        header_keys=tuple(header_keys),
        payload_object=bool(row.get("payload_object", False)),
        payload_claims=frozenset(claims),
        left=_boundary(row, row.get("left_boundary", "b64url_dot")),
        right=_boundary(row, row.get("right_boundary", "b64url_dot")),
        allow_values=(
            frozenset(value.rsplit(".", 1)[0] for value in _strings(row, "allow_values"))
            if "allow_values" in row
            else frozenset()
        ),
    )


def _build_pair(row: Mapping[str, Any]) -> PairRow:
    return PairRow(
        id=row["id"],
        tier=row["tier"],
        chars=_alphabet(row, row.get("alphabet")),
        length=_int(row, "length", 1),
        left=_boundary(row, row.get("left_boundary")),
        right=_boundary(row, row.get("right_boundary")),
        pair_prefixes=_strings(row, "pair_prefixes"),
        window=_int(row, "window", 1),
        allow_suffixes=tuple(row.get("allow_suffixes", ())),
    )


def _build_armour(row: Mapping[str, Any]) -> ArmourRow:
    type_max = _int(row, "type_max", 0)
    type_chars = _alphabet(row, row["type_alphabet"]) if type_max else ""
    for field in ("begin", "suffix", "end", "unterminated_id"):
        if not isinstance(row.get(field), str) or not row[field]:
            raise _fail(row, f"{field} must be a non-empty string")
    return ArmourRow(
        id=row["id"],
        unterminated_id=row["unterminated_id"],
        tier=row["tier"],
        begin=row["begin"],
        type_chars=type_chars,
        type_max=type_max,
        suffix=row["suffix"],
        end=row["end"],
        max_block=_int(row, "max_block", 1),
    )


def _build_known_key(row: Mapping[str, Any]) -> KnownKeyRow:
    inputs = frozenset(_strings(row, "inputs"))
    if not inputs <= _INPUTS:
        raise _fail(row, f"unknown input(s) {sorted(inputs - _INPUTS)}")
    gate = row.get("gate")
    if gate not in _GATES:
        raise _fail(row, f"unknown gate {gate!r}")
    return KnownKeyRow(
        id=row["id"], tier=row["tier"], keys=_strings(row, "keys"), inputs=inputs, gate=gate
    )


def _build_structure(row: Mapping[str, Any]) -> StructureRow:
    kind = row.get("kind")
    if kind not in _KINDS and kind not in VALUE_KINDS:
        raise _fail(row, f"unknown kind {kind!r}")
    if row["tier"] != IDENTIFIER:
        raise _fail(row, "a structure row is Identifier tier")
    needs = VALUE_KINDS.get(kind, _KIND_NEEDS.get(kind, frozenset()))
    stray = (set(row) & (_VALUE_FIELDS | {"params"})) - needs
    if stray:
        raise _fail(row, f"field(s) {sorted(stray)} do not apply to kind {kind!r}")
    params = frozenset(_strings(row, "params")) if "params" in needs else frozenset()
    if params & NEVER_MASK_PARAMS:
        raise _fail(row, f"credential parameter(s) {sorted(params & NEVER_MASK_PARAMS)}")
    labels = _strings(row, "labels") if "labels" in needs else ()
    if kind == "labelled_value" and any(lab != lab.lower() for lab in labels):
        raise _fail(row, "labels are matched case-insensitively and must be lower case")
    max_length = _int(row, "max_length", 1)
    min_length = _int(row, "min_length", 1) if "min_length" in needs else 0
    if min_length > max_length:
        raise _fail(row, "min_length must not exceed max_length")
    lengths: frozenset[int] = frozenset()
    if "lengths" in needs:
        ls = row.get("lengths")
        if (
            not isinstance(ls, list)
            or not ls
            or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 < x <= max_length for x in ls
            )
        ):
            raise _fail(row, "lengths must be positive integers no longer than max_length")
        lengths = frozenset(ls)
    return StructureRow(
        id=row["id"],
        tier=row["tier"],
        kind=kind,
        params=params,
        max_length=max_length,
        labels=labels,
        values=_strings(row, "values") if "values" in needs else (),
        chars=_alphabet(row, row.get("alphabet")) if "alphabet" in needs else "",
        min_length=min_length,
        pad=_int(row, "pad", 0) if "pad" in needs else 0,
        lengths=lengths,
    )


def _build_url_param(row: Mapping[str, Any]) -> UrlParamRow:
    key = row.get("key")
    if not isinstance(key, str) or key not in URL_PARAM_KEYS:
        raise _fail(
            row, f"url_param key {key!r} is not in URL_PARAM_KEYS (a value that grants access)"
        )
    if row["tier"] != SECRET:
        raise _fail(row, "a url_param row is Secret tier")
    return UrlParamRow(id=row["id"], tier=row["tier"], key=key)


def _build_url_userinfo(row: Mapping[str, Any]) -> UrlUserinfoRow:
    if row["tier"] != SECRET:
        raise _fail(row, "a url_userinfo row is Secret tier")
    return UrlUserinfoRow(id=row["id"], tier=row["tier"])


_HEADER_READERS = frozenset(
    {"header", "cookie", "curl_user", "mysql_password", "docker_login", "netrc", "bearer"}
)
#: Readers that anchor on their own fixed flags or keyword and read no ``names``.
_NAMELESS_READERS = frozenset({"curl_user", "netrc", "bearer"})


def _build_header(row: Mapping[str, Any]) -> HeaderRow:
    if row["tier"] != SECRET:
        raise _fail(row, "a header row is Secret tier")
    reader = row.get("reader")
    if reader not in _HEADER_READERS:
        raise _fail(row, f"unknown reader {reader!r}")
    if reader in _NAMELESS_READERS:
        if "names" in row:
            raise _fail(row, f"reader {reader!r} reads no names")
        names: tuple[str, ...] = ()
    else:
        names = tuple(n.lower() for n in _strings(row, "names"))
    scheme = row.get("scheme", "")
    if not isinstance(scheme, str):
        raise _fail(row, "scheme must be a string")
    allow = (
        frozenset(v.rsplit(".", 1)[0] for v in _strings(row, "allow_values"))
        if "allow_values" in row
        else frozenset()
    )
    return HeaderRow(
        id=row["id"],
        tier=row["tier"],
        reader=reader,
        names=names,
        scheme=scheme.lower(),
        min_length=_int(row, "min_length", 1),
        allow_values=allow,
    )


def _build_url_path_secret(row: Mapping[str, Any]) -> UrlPathSecretRow:
    if row["tier"] != SECRET:
        raise _fail(row, "a url_path_secret row is Secret tier")
    prefix = row.get("path_prefix")
    if not isinstance(prefix, str) or not prefix.startswith("/") or not prefix.endswith("/"):
        raise _fail(row, "path_prefix must start and end with '/'")
    hosts = frozenset(h.lower() for h in _strings(row, "hosts"))
    return UrlPathSecretRow(
        id=row["id"],
        tier=row["tier"],
        hosts=hosts,
        path_prefix=prefix,
        segment=_int(row, "segment", 1),
    )


def _build_encoded(row: Mapping[str, Any]) -> EncodedRow:
    if row["tier"] != SECRET:
        raise _fail(row, "an encoded row is Secret tier")
    return EncodedRow(id=row["id"], tier=row["tier"], min_length=_int(row, "min_length", 8))


_BUILDERS = {
    "token_format": _build_format,
    "jose": _build_jose,
    "key_id_pair": _build_pair,
    "armour": _build_armour,
    "known_key": _build_known_key,
    "url_param": _build_url_param,
    "url_userinfo": _build_url_userinfo,
    "encoded": _build_encoded,
    "url_path_secret": _build_url_path_secret,
    "header": _build_header,
    "structure": _build_structure,
}


def build_table(rows: Sequence[Mapping[str, Any]]) -> Table:
    """Validate every row and build the detector inputs. Refuses unknown fields."""
    built: dict[str, list[Any]] = {name: [] for name in _BUILDERS}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise TableError("a row must be a mapping")
        detector = row.get("detector")
        if detector not in _FIELDS:
            raise _fail(row, f"unknown detector {detector!r}")
        unknown = set(row) - _FIELDS[detector]
        if unknown:
            raise _fail(row, f"unknown field(s) {sorted(unknown)}")
        rule_id = row.get("id")
        if not isinstance(rule_id, str) or not rule_id or not set(rule_id) <= _ID_CHARS:
            raise _fail(row, "id must be lower-case letters, digits, '.' and '_'")
        if rule_id in seen:
            raise _fail(row, "duplicate id")
        seen.add(rule_id)
        if row.get("tier") not in TIERS:
            raise _fail(row, f"unknown tier {row.get('tier')!r}")
        built[detector].append(_BUILDERS[detector](row))
    return Table(
        formats=tuple(built["token_format"]),
        jose=tuple(built["jose"]),
        pairs=tuple(built["key_id_pair"]),
        armour=tuple(built["armour"]),
        known_keys=tuple(built["known_key"]),
        structures=tuple(built["structure"]),
        url_params=tuple(built["url_param"]),
        userinfo=tuple(built["url_userinfo"]),
        encoded=tuple(built["encoded"]),
        path_secrets=tuple(built["url_path_secret"]),
        headers=tuple(built["header"]),
    )


TABLE = build_table(ROWS)
