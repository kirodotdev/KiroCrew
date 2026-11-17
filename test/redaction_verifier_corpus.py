"""The independent verifier's corpus for the redaction redesign, as data.

Gathered by the independent verification of the rule table: 175 benign daily-use
snippets (``BENIGN``), 43 realistic leaks with the exact secret values each holds
(``LEAKS``), and 7 ambiguous documentation literals (``AMBIGUOUS``). Every value is
fake, built at import from one seeded generator, so the source holds no credential;
private-key armour names are joined at run time so repo content scanners do not read
this file as holding a key. The two certificates are public test certificates.
"""

from __future__ import annotations

import base64
from urllib.parse import quote

from redaction_verifier_benign import BENIGN
from redaction_verifier_gen import (
    UPD,
    R,
    aws_secret,
    b62,
    b64s,
    b64u,
    ghp,
    hexs,
    jwt,
    key_id,
    pem_body,
    pick,
    uuid,
)

__all__ = ["AMBIGUOUS", "BENIGN", "LEAKS"]

_PRIVATE_KEY = "PRIVATE" + " KEY"

ACCT = "111122223333"
L = []


def leak(cat, text, *secrets):
    for s in secrets:
        assert s in text, (cat, s)
    L.append((f"L{len(L)+1:02d}", cat, text, list(secrets)))


KID = key_id("AKIA")
SEC = aws_secret()
leak(
    "dotenv-aws",
    f"""# .env
AWS_ACCESS_KEY_ID={KID}
AWS_SECRET_ACCESS_KEY={SEC}
AWS_DEFAULT_REGION=us-west-2
""",
    SEC,
)

KID2 = key_id("ASIA")
SEC2 = aws_secret()
TOK2 = "IQoJb3JpZ2luX2VjE" + b64s(380)
leak(
    "aws-credentials-file",
    f"""[kc-dev]
aws_access_key_id = {KID2}
aws_secret_access_key = {SEC2}
aws_session_token = {TOK2}
region = us-west-2
""",
    SEC2,
    TOK2,
)

KID3 = key_id("ASIA")
SEC3 = aws_secret()
TOK3 = "FwoGZXIvYXdzE" + b64s(300)
leak(
    "sts-assume-role",
    f"""{{
    "Credentials": {{
        "AccessKeyId": "{KID3}",
        "SecretAccessKey": "{SEC3}",
        "SessionToken": "{TOK3}",
        "Expiration": "2026-11-14T08:31:05+00:00"
    }},
    "AssumedRoleUser": {{
        "AssumedRoleId": "AROA{pick(UPD, 17)}:alice",
        "Arn": "arn:aws:sts::{ACCT}:assumed-role/Admin/alice"
    }}
}}""",
    SEC3,
    TOK3,
)

OPQ = b64u(48)
leak(
    "curl-bearer-opaque",
    f"""curl -sS -H "Authorization: Bearer {OPQ}" https://api.internal.example.com/v2/orders?page=1""",
    OPQ,
)

J = jwt(
    {
        "sub": "u_" + b62(12),
        "iss": "https://auth.example.com",
        "aud": "kc-api",
        "exp": 1763120000,
        "scope": "orders:read orders:write",
    }
)
leak(
    "curl-bearer-jwt",
    f"""$ curl -i https://api.example.com/v1/me -H 'Authorization: Bearer {J}'
HTTP/2 200""",
    J,
)

PEM = pem_body(25)
leak(
    "pem-rsa",
    f"""-----BEGIN {_PRIVATE_KEY}-----
{PEM}
-----END {_PRIVATE_KEY}-----""",
    PEM,
)

OSSH = pem_body(7, head="b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW")
leak(
    "pem-openssh",
    f"""Here is the deploy key from the old box:
-----BEGIN OPENSSH {_PRIVATE_KEY}-----
{OSSH}
-----END OPENSSH {_PRIVATE_KEY}-----""",
    OSSH,
)

PW1 = "Tr0ub4dor-" + b62(10)
leak(
    "postgres-url",
    f"""DATABASE_URL=postgres://kc_admin:{PW1}@kc-prod.cluster-{b62(12).lower()}.us-west-2.rds.amazonaws.com:5432/kc""",
    PW1,
)

PW2 = b62(16)
leak(
    "mongodb-url",
    f"""client = MongoClient("mongodb+srv://app:{PW2}@cluster0.{b62(5).lower()}.mongodb.net/kc?retryWrites=true&w=majority")""",
    PW2,
)

PW3 = b62(20)
leak(
    "redis-url",
    f"""REDIS_URL=redis://:{PW3}@kc-cache.{b62(6).lower()}.ng.0001.usw2.cache.amazonaws.com:6379/0""",
    PW3,
)

PW4 = "S3cure!" + b62(9)
leak(
    "yaml-password",
    f"""database:
  host: kc-prod.internal
  user: kc_admin
  password: {PW4}
  pool_size: 10
""",
    PW4,
)

PW5 = b62(14) + "#9"
leak(
    "properties-password",
    f"""spring.datasource.url=jdbc:postgresql://kc-prod.internal:5432/kc
spring.datasource.username=kc_admin
spring.datasource.password={PW5}
""",
    PW5,
)

XOXB = f"xoxb-{pick('0123456789', 12)}-{pick('0123456789', 13)}-{b62(24)}"
leak("slack-xoxb", f"""client = WebClient(token="{XOXB}")""", XOXB)

GHP = ghp()
leak(
    "github-ghp",
    f"""export GITHUB_TOKEN={GHP}
gh auth status""",
    GHP,
)

GHPAT = "github_pat_" + b62(22) + "_" + b62(59)
leak(
    "github-fine-grained",
    f"""git clone https://octocat:{GHPAT}@github.com/kirodotdev/private-fixtures.git""",
    GHPAT,
)

OAI = "sk-proj-" + b64u(48) + "T3BlbkFJ" + b64u(48)
leak("openai-key", f"""OPENAI_API_KEY={OAI}""", OAI)

ANT = "sk-ant-api03-" + b64u(93) + "AA"
leak("anthropic-key", f"""client = anthropic.Anthropic(api_key="{ANT}")""", ANT)

SIG = hexs(64)
leak(
    "presigned-s3-akia",
    f"""Download (valid 1 hour): https://kc-artifacts-{ACCT}-us-west-2.s3.us-west-2.amazonaws.com/builds/report.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={key_id('AKIA')}%2F20261114%2Fus-west-2%2Fs3%2Faws4_request&X-Amz-Date=20261114T073000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature={SIG}""",
    SIG,
)

SIG2 = hexs(64)
STOK = "IQoJb3JpZ2luX2VjE" + b64s(400)
STOKQ = quote(STOK, safe="")
leak(
    "presigned-s3-asia",
    f"""https://kc-artifacts-{ACCT}-us-west-2.s3.us-west-2.amazonaws.com/exports/transcript.zip?response-content-disposition=attachment%3B%20filename%3D%22transcript.zip%22&X-Amz-Security-Token={STOKQ}&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20261114T073000Z&X-Amz-SignedHeaders=host&X-Amz-Expires=300&X-Amz-Credential={key_id('ASIA')}%2F20261114%2Fus-west-2%2Fs3%2Faws4_request&X-Amz-Signature={SIG2}""",
    SIG2,
    STOKQ,
)

BASIC = base64.b64encode(f"svc-kc:{b62(18)}".encode()).decode()
leak(
    "basic-auth-header",
    f"""> GET /rest/api/3/myself HTTP/1.1
> Host: example.atlassian.net
> Authorization: Basic {BASIC}
> Accept: application/json""",
    BASIC,
)

SESS = b64u(43)
leak(
    "set-cookie-session",
    f"""HTTP/1.1 302 Found
Location: /dashboard
Set-Cookie: session={SESS}; Path=/; HttpOnly; Secure; SameSite=Lax""",
    SESS,
)

PW6 = "Wint3r-" + b62(10)
leak(
    "curl-u",
    f"""curl -u admin:{PW6} https://jenkins.internal.example.com/job/kc-release/build -X POST""",
    PW6,
)

PW7 = ghp()
PW7b = b62(18)
leak(
    "netrc",
    f"""machine github.com
  login octocat
  password {PW7}
machine artifactory.internal.example.com login ci password {PW7b}""",
    PW7,
    PW7b,
)

SLK = f"T0{pick(UPD, 8)}/B0{pick(UPD, 9)}/{b62(24)}"
SLKS = SLK.split("/")[-1]
leak(
    "slack-webhook",
    f"""SLACK_WEBHOOK_URL=https://hooks.slack.com/services/{SLK}""",
    SLKS,
)

DWH = b64u(68)
leak(
    "discord-webhook",
    f"""https://discord.com/api/webhooks/{pick('0123456789', 19)}/{DWH}""",
    DWH,
)

STR = "sk_live_" + b62(99)
leak("stripe-live", f"""stripe.api_key = "{STR}" """, STR)

GAK = "AIza" + b64u(35)
leak(
    "google-api-key",
    f"""https://maps.googleapis.com/maps/api/geocode/json?address=Seattle&key={GAK}""",
    GAK,
)

AZK = base64.b64encode(bytes(R.randrange(256) for _ in range(64))).decode()
leak(
    "azure-conn-string",
    f"""AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=kcexports;AccountKey={AZK};EndpointSuffix=core.windows.net\"""",
    AZK,
)

NPM = "npm_" + b62(36)
leak(
    "npmrc",
    f"""//registry.npmjs.org/:_authToken={NPM}
@kiro:registry=https://registry.npmjs.org/""",
    NPM,
)

PYPI = "pypi-AgEIcHlwaS5vcmc" + b64u(150)
leak(
    "pypi-token",
    f"""[pypi]
  username = __token__
  password = {PYPI}""",
    PYPI,
)

K8SPW = base64.b64encode(("Db-" + b62(16)).encode()).decode()
leak(
    "k8s-secret-yaml",
    f"""$ kubectl get secret kc-db -o yaml
apiVersion: v1
data:
  password: {K8SPW}
  username: a2NfYWRtaW4=
kind: Secret
metadata:
  name: kc-db
  namespace: prod
type: Opaque""",
    K8SPW,
)

PW8 = "Rt" + b62(12)
leak(
    "mysql-glued-p",
    f"""$ mysql -h kc-prod.internal -u root -p{PW8} -e 'SHOW DATABASES;'""",
    PW8,
)

PW9 = b62(20)
leak(
    "docker-login-p",
    f"""docker login -u kcbot -p {PW9} registry.internal.example.com""",
    PW9,
)

XAK = b62(40)
leak(
    "x-api-key-header",
    f"""curl https://api.internal.example.com/v1/usage -H "x-api-key: {XAK}" -H "accept: application/json\"""",
    XAK,
)

GL = "glpat-" + b64u(20)
leak(
    "gitlab-pat",
    f"""git remote set-url origin https://oauth2:{GL}@gitlab.example.com/kc/tools.git""",
    GL,
)

SAS = quote(base64.b64encode(bytes(R.randrange(256) for _ in range(32))).decode(), safe="")
leak(
    "azure-sas",
    f"""https://kcexports.blob.core.windows.net/exports/report.csv?sv=2022-11-02&ss=b&srt=o&sp=r&se=2026-11-15T07:30:00Z&st=2026-11-14T07:30:00Z&spr=https&sig={SAS}""",
    SAS,
)

TG = f"{pick('0123456789', 10)}:AA{b64u(33)}"
leak("telegram-bot", f"""TELEGRAM_BOT_TOKEN={TG}""", TG)

JUP = hexs(48)
leak(
    "jupyter-token",
    f"""    To access the server, open this file in a browser:
        file:///home/dev/.local/share/jupyter/runtime/jpserver-4242-open.html
    Or copy and paste one of these URLs:
        http://127.0.0.1:8888/lab?token={JUP}""",
    JUP,
)

SMV = "Pg-" + b62(18)
leak(
    "secretsmanager-get-value",
    f"""{{
    "ARN": "arn:aws:secretsmanager:us-west-2:{ACCT}:secret:prod/db/password-{b62(6)}",
    "Name": "prod/db/password",
    "VersionId": "{uuid()}",
    "SecretString": "{{\\"username\\":\\"kc_admin\\",\\"password\\":\\"{SMV}\\"}}",
    "VersionStages": ["AWSCURRENT"]
}}""",
    SMV,
)

CS = "GOCSPX-" + b64u(28)
leak(
    "google-client-secret-json",
    f"""{{"installed":{{"client_id":"812345678901-{b62(32).lower()}.apps.googleusercontent.com","project_id":"kc-dev","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","client_secret":"{CS}","redirect_uris":["http://localhost"]}}}}""",
    CS,
)

HF = "hf_" + b62(34)
leak("huggingface", f"""huggingface-cli login --token {HF}""", HF)

ECRAUTH = base64.b64encode(("AWS:" + "eyJwYXlsb2FkIjoi" + b64s(200)).encode()).decode()
leak(
    "ecr-auth-token",
    f"""{{
    "authorizationData": [{{
        "authorizationToken": "{ECRAUTH}",
        "expiresAt": "2026-11-14T19:30:00+00:00",
        "proxyEndpoint": "https://{ACCT}.dkr.ecr.us-west-2.amazonaws.com"
    }}]
}}""",
    ECRAUTH,
)

SNT = "sntrys_" + b64u(60)
TWP = hexs(32)
JWS = b62(48)
leak(
    "env-generic-token",
    f"""# .env.production
SENTRY_AUTH_TOKEN={SNT}
TWILIO_AUTH_TOKEN={TWP}
JWT_SECRET={JWS}
""",
    SNT,
    TWP,
    JWS,
)

A = []


def amb(cat, text, note):
    A.append((f"A{len(A)+1:02d}", cat, text, note))


amb(
    "oauth-callback-code",
    f"http://localhost:8085/callback?state={b64u(32)}&code=4%2F0A{b64u(70)}&scope=email%20profile%20openid&authuser=0&prompt=consent",
    "OAuth callback: code= is short-lived and single-use",
)
amb(
    "docker-docs-literal",
    "docker run --name some-mysql -e MYSQL_ROOT_PASSWORD=my-secret-pw -d mysql:8.4",
    "the official MySQL image docs literal",
)
amb(
    "test-literal",
    'assert redact("password=hunter2") != "password=hunter2"',
    "a test fixture literal",
)
amb(
    "doc-basic-header",
    "Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQ=",
    "base64 of username:password, a doc example",
)
amb(
    "jwtio-new-default",
    jwt(
        {"sub": "1234567890", "name": "John Doe", "admin": True, "iat": 1516239022},
        secret="a-string-secret-at-least-256-bits-long",
    ),
    "jwt.io current default (HS256, published secret)",
)
amb(
    "aws-doc-session-token",
    '"SessionToken": "AQoDYXdzEJr...<remainder of session token>"',
    "AWS docs elision",
)
amb(
    "csrf-hidden-input",
    f'<input type="hidden" name="csrf_token" value="{b64u(43)}">',
    "CSRF token in page source",
)

#: ``(id, category, text, secret values)``
LEAKS: list[tuple[str, str, str, list[str]]] = L
#: ``(id, category, text, note)``
AMBIGUOUS: list[tuple[str, str, str, str]] = A
