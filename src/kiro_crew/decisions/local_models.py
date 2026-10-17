"""Local System One models the decision seam can use instead of hosted Jev.

A local model is an open-weight decision server the owner runs on this machine,
speaking the same ``/v1/systemone`` wire format Jev does, so :class:`JevOracle`
talks to it unchanged. This module owns three things:

* the PRESETS the dashboard offers, each with the numbers a reader needs to
  choose one -- how close it comes to Jev, how much memory it needs, how slow it
  is on a CPU;
* :func:`is_loopback_endpoint`, the one test for "this endpoint is on this
  machine", which ``impl_jev`` uses to withhold the Jev key and the provider
  route uses to decide whether a switch may carry consent across;
* :func:`endpoint_for`, which builds a preset's endpoint from a port, so no
  caller ever composes a local URL from caller-supplied text;
* what the gateway needs to RUN a preset itself (:mod:`.local_runtime`): the
  pinned weight files on Kiro Crew's model CDN, the dependency lock and the
  launcher, both shipped beside this module in ``local_servers/``.

The quality numbers are the share of Jev's correct answers each model matched on
the same 231 public JevBench items (easy, original and hard tiers), measured on a
10-core CPU. Jev answered 200 of them correctly (48/48, 71/72, 81/111); the hard
tier gets its own number because it is where the models separate. They are
measurements of one host, not guarantees, and the card says so.
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import yarl

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT
from kiro_crew.security import canonicalize_ip

#: Preset id that means "hosted Jev at the shipped default endpoint".
PRESET_JEV = "jev"

#: Preset id, and the endpoint the provider route writes for it, meaning "no decision
#: model": nothing is sent, no local server runs, and every point uses its built-in
#: rules. Not a URL, so no client can dial it.
PRESET_NONE = "none"
ENDPOINT_NONE = "none"

#: The path every System One server answers on.
SYSTEMONE_PATH = "/v1/systemone"

#: Local ports the provider route accepts. Below 1024 needs privileges no model
#: server should run with; the upper bound is the TCP limit.
PORT_MIN = 1024
PORT_MAX = 65535


#: Where the pinned weights are served. A tampered object can only fail the sha256
#: pin, which is the trust anchor; the CDN is where bytes come from, not why they
#: are trusted.
MODEL_CDN = "https://dostcaqwrph3m.cloudfront.net"

#: A preset is recommended only on a machine with at least this many times its
#: peak memory, so running it never squeezes the owner's other sessions.
RECOMMEND_RAM_FACTOR = 4


@dataclass(frozen=True)
class ModelFile:
    """One weight file: its path under the model directory, sha256 and size.

    *source*, when set, is the file's CDN path relative to :data:`MODEL_CDN`, for a
    file mirrored from a different upstream repository than the preset's own --
    the base model an adapter checkpoint is trained on. Empty means the preset's
    ``<id>/<revision>/<path>``.
    """

    path: str
    sha256: str
    size: int
    source: str = ""


@dataclass(frozen=True)
class LocalModel:
    """One local model the dashboard offers."""

    id: str
    name: str
    #: Model id sent in the request; local servers route on it or ignore it.
    model: str
    default_port: int
    #: Correct answers matched, as a percentage of Jev's, over all public items.
    jev_relative_pct: int
    #: The same ratio on the hard tier alone.
    hard_relative_pct: int
    #: Peak resident memory of the server, measured, in GB.
    peak_ram_gb: float
    #: Median seconds per decision on 10 CPU cores.
    p50_secs: float
    #: 95th-percentile seconds per decision on 10 CPU cores.
    p95_secs: float
    #: ``provider.timeout_ms`` written when the preset is chosen. Each decision
    #: point still clamps its own wait, so a slow answer degrades to "no decision".
    timeout_ms: int
    #: Upstream commit of the weights; also the directory they are served from.
    revision: str
    #: Every file the server reads, pinned. Nothing else is downloaded.
    files: tuple[ModelFile, ...]
    #: Launcher and dependency lock, by name under ``local_servers/``.
    launcher: str
    requirements: str

    @property
    def recommended_total_ram_gb(self) -> int:
        """Total machine memory at or above which the dashboard recommends this preset.

        :data:`RECOMMEND_RAM_FACTOR` times the server's peak, so the model takes at
        most a quarter of the machine and the owner's other sessions keep the rest.
        The card picks the first preset, in :data:`LOCAL_MODELS` order, whose
        threshold the machine meets, and hosted Jev when none is met.
        """
        return math.ceil(self.peak_ram_gb * RECOMMEND_RAM_FACTOR)

    @property
    def download_bytes(self) -> int:
        """Bytes of weights to fetch; the environment adds about 1 GB more."""
        return sum(f.size for f in self.files)

    def file_url(self, f: ModelFile) -> str:
        """The CDN url of *f*."""
        if f.source:
            return f"{MODEL_CDN}/{f.source}"
        return f"{MODEL_CDN}/{self.id}/{self.revision}/{f.path}"


#: Recommended first: the best quality at a memory cost most workstations have.
LOCAL_MODELS: tuple[LocalModel, ...] = (
    LocalModel(
        id="plumb-4b",
        name="Plumb-4B",
        model="plumb-4b",
        default_port=8102,
        jev_relative_pct=103,
        hard_relative_pct=109,
        peak_ram_gb=14.8,
        p50_secs=2.4,
        p95_secs=32.0,
        timeout_ms=5000,
        revision="24f7bf77e7ee258a2d158c61ea2dce2b60321010",
        files=(
            ModelFile(
                "LICENSE", "3ddf9be5c28fe27dad143a5dc76eea25222ad1dd68934a047064e56ed2fa40c5", 11560
            ),
            ModelFile(
                "NOTICE", "f039a4e32f0a1f22da6ca65055a3bb6fa99cf33fe5d7dcec4e156a27a11d2ab5", 701
            ),
            ModelFile(
                "NOTICE-jevk5",
                "53df231ab1ada48bce57d4b9f7314c7e7db1f30369f852c1c946ba7637e5b614",
                1423,
            ),
            ModelFile(
                "chat_template.jinja",
                "a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715",
                7756,
            ),
            ModelFile(
                "config.json",
                "63f47812d0f11118e4d252d2b3ad488707eb9287a11589f4fd382a1d31182724",
                1979,
            ),
            ModelFile(
                "generation_config.json",
                "62153eb6c69f2e1f426beaa8002b7186437e949c7588167085df14e10e9c0a73",
                116,
            ),
            ModelFile(
                "jevk5_config.json",
                "a971c01fcf3ec161a03c61f5e3883b93fbd27696a529a54acd989845bf6fdead",
                22,
            ),
            ModelFile(
                "model.safetensors",
                "89e119ea07f4c5b4b6715560c7de6694ec3b777dfd0e62351da0b33986d2e1e1",
                8411558400,
            ),
            ModelFile(
                "tokenizer.json",
                "06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523",
                19989325,
            ),
            ModelFile(
                "tokenizer_config.json",
                "9cf04fffe3d8c3b85e439fb35c7acad0761ab51c422a8c4256d9f887c3a0be7d",
                1125,
            ),
        ),
        launcher="plumb_cpu.py",
        requirements="plumb-4b.requirements.txt",
    ),
    LocalModel(
        id="strands-decider-2b",
        name="Strands Decider 2B",
        model="strands-decider-2b",
        default_port=8106,
        jev_relative_pct=84,
        hard_relative_pct=69,
        peak_ram_gb=12.0,
        p50_secs=0.64,
        p95_secs=11.7,
        timeout_ms=5000,
        revision="bb282d786bc251fd4e3068de3ada9ddbb38127cd",
        # The LoRA adapter and head, plus the Qwen3.5-2B base they adapt, mirrored
        # under ``base/``: the checkpoint names its base by Hub id, and the server
        # would otherwise fetch it from the Hub on first start.
        files=(
            ModelFile(
                "LICENSE.md",
                "95e36511c33366a2105047cae24c04a60f09c4c755662050986eaf5547bf6fe9",
                13079,
            ),
            ModelFile(
                "NOTICE",
                "3736ca5d7bfabf07ffdb2ad03fa92764c1a47cbb9649d97b92e7a505103be958",
                456,
            ),
            ModelFile(
                "chat_template.jinja",
                "273d8e0e683b885071fb17e08d71e5f2a5ddfb5309756181681de4f5a1822d80",
                7755,
            ),
            ModelFile(
                "head.safetensors",
                "daad0727152b6185447cee36f78230c144312feb7b19f02749b19645238b5287",
                4213224,
            ),
            ModelFile(
                "hobson_config.json",
                "2ae86f2ed56975f68e8f2f368104df8ce0ff4b7d9d146dcaf8eec5626d62449d",
                743,
            ),
            ModelFile(
                "lora/adapter_config.json",
                "eb48e4ff81569664c4dd2a504b53da598eeaa390c265c2269ce4fe7526eab38a",
                1271,
            ),
            ModelFile(
                "lora/adapter_model.safetensors",
                "701bdb895887097f7954ec7eb06f7937d3b790035b5462195eb26abd80a4aebc",
                67324872,
            ),
            ModelFile(
                "provenance.json",
                "eb9ab7e029d9cc6718a6302a3edc6085bbb42c4ee42284b8ee7727f0ab4416a1",
                970,
            ),
            ModelFile(
                "tokenizer.json",
                "a2cdd2e108566b09079afa8d266e9e65e7c280f27b771218237a06de5ba9cd86",
                19989592,
            ),
            ModelFile(
                "tokenizer_config.json",
                "8671bed7c852ce9e661be94f179a7b4ffd091c2a65aea0363e5501c20318ee45",
                1128,
            ),
            ModelFile(
                "train_config.json",
                "5d15c863a3b83103d4d59068a8341b61745b10079d6f8509fab537415d1eb31e",
                1465,
            ),
            ModelFile(
                "base/LICENSE",
                "50cbab8a892c5f2993b8c7351a99182507472def3b1374558308605d99b86b32",
                11343,
                source="qwen3.5-2b-base/b1485b2fa6dfa1287294f269f5fb618e03d52d7c/LICENSE",
            ),
            ModelFile(
                "base/NOTICE",
                "18b57614497ebdb5811d9e9963a57ca0d6f005aaf1f2f4c923df857a49d2eefa",
                286,
                source="qwen3.5-2b-base/b1485b2fa6dfa1287294f269f5fb618e03d52d7c/NOTICE",
            ),
            ModelFile(
                "base/config.json",
                "ed1c1723241f23f7f4e23430759cbd7dcfb4103cbdfe052bfe7626b57c2615b4",
                2908,
                source="qwen3.5-2b-base/b1485b2fa6dfa1287294f269f5fb618e03d52d7c/config.json",
            ),
            ModelFile(
                "base/model.safetensors-00001-of-00001.safetensors",
                "928acbf11878c32185bbd863514d191769285065ab9ea14fbfe431303f5fdf2d",
                4548221488,
                source="qwen3.5-2b-base/b1485b2fa6dfa1287294f269f5fb618e03d52d7c/model.safetensors-00001-of-00001.safetensors",
            ),
            ModelFile(
                "base/model.safetensors.index.json",
                "74d2ddfe79f10f35b27b498632f02b97b60dd9ec39b35d7c5a890c399284e319",
                64460,
                source="qwen3.5-2b-base/b1485b2fa6dfa1287294f269f5fb618e03d52d7c/model.safetensors.index.json",
            ),
        ),
        launcher="strands_decider_cpu.py",
        requirements="strands-decider-2b.requirements.txt",
    ),
    LocalModel(
        id="laya",
        name="Laya",
        model="english",
        default_port=8104,
        jev_relative_pct=67,
        hard_relative_pct=47,
        peak_ram_gb=6.0,
        p50_secs=0.17,
        p95_secs=0.51,
        timeout_ms=2000,
        revision="55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851",
        # The English checkpoint only: the repository also bundles multilingual and
        # typed-decision checkpoints the launcher never loads.
        files=(
            ModelFile(
                "LICENSE", "3ddf9be5c28fe27dad143a5dc76eea25222ad1dd68934a047064e56ed2fa40c5", 11560
            ),
            ModelFile(
                "NOTICE", "9258353699076c6b083c9b791fb55306b11952e52b1c6e9741b68a39bbea0bea", 285
            ),
            ModelFile(
                "encoder/config.json",
                "bf3ab80598fdccf414855a2ce80f22859e4492d06ca8a62ddd1cfb63972f8979",
                2083,
            ),
            ModelFile(
                "model.safetensors",
                "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c",
                842609210,
            ),
            ModelFile(
                "rl_agent_config.json",
                "ae287b56bbcf5f8c4f4541ae9dfd00c914c4c48b940b8398c3058af37ba92bbd",
                745,
            ),
            ModelFile(
                "tokenizer/tokenizer.json",
                "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
                3583228,
            ),
            ModelFile(
                "tokenizer/tokenizer_config.json",
                "50044de60daaa73df97d262e15a40d4faf0160e7d742df64b377877a1320dd12",
                308,
            ),
        ),
        launcher="laya_cpu.py",
        requirements="laya.requirements.txt",
    ),
)

_BY_ID = {m.id: m for m in LOCAL_MODELS}


def get(preset_id: object) -> LocalModel | None:
    """The preset named *preset_id*, or ``None`` for anything else."""
    return _BY_ID.get(preset_id) if isinstance(preset_id, str) else None


def endpoint_for(port: int) -> str:
    """The loopback endpoint of a local server on *port*. Raises on a bad port."""
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise ValueError("port out of range")
    return f"http://127.0.0.1:{port}{SYSTEMONE_PATH}"


def is_loopback_endpoint(endpoint: object) -> bool:
    """Whether *endpoint* is an http(s) URL on a LITERAL loopback address.

    Literal addresses only: ``localhost`` is a name, and a hosts file or resolver
    decides where a name goes, so it is not proof the request stays on this
    machine. User info in the authority is refused too, since it is how a URL
    that reads as loopback is made to parse as another host. A missing port is
    still local: it means the scheme's default port on the same address, and
    reading it as remote would hand the Jev key to whatever holds port 80 here.
    """
    if not isinstance(endpoint, str):
        return False
    try:
        parts = urlsplit(endpoint.strip())
        parts.port  # raises on an out-of-range or non-numeric port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return False
    # The host is read the way aiohttp dials it: ``yarl.URL.raw_host`` applies IDNA
    # normalisation, which turns ``127。0。0。1``, fullwidth dots and circled digits
    # into ``127.0.0.1``. Then the same canonicalisation the browser and link-unfurl
    # host checks use: every spelling the resolver reads as a number without a
    # lookup -- ``127.1``, ``0x7f.1``, ``2130706433``, ``[::ffff:127.0.0.1]`` --
    # becomes the dotted quad it reaches. A name is left as it is and so is never
    # resolved.
    try:
        dialed = yarl.URL(endpoint.strip()).raw_host or ""
        addr = ipaddress.ip_address(canonicalize_ip(dialed))
    except ValueError:
        return False
    # The unspecified address is dialled as this machine: connecting to 0.0.0.0 or
    # [::] reaches whatever listens on the local port, so it is local for the key.
    return addr.is_loopback or addr.is_unspecified


def _is_route_built(endpoint: object) -> bool:
    """Whether *endpoint* has exactly the shape :func:`endpoint_for` builds."""
    if not isinstance(endpoint, str):
        return False
    try:
        # ``urlsplit`` itself raises on an unbalanced ``[`` in a hand-written address.
        port = urlsplit(endpoint.strip()).port
    except ValueError:
        return False
    if port is None or not PORT_MIN <= port <= PORT_MAX:
        return False
    return endpoint.strip() == endpoint_for(port)


def active_id(endpoint: object, model: object) -> str:
    """Which preset the configured provider matches: a local id, ``jev``, ``none``, or ``custom``.

    A preset is only ever the address the provider route builds itself; any other
    loopback spelling is a hand-written address and is reported as ``custom``, so
    the card keeps showing where it is set and that no key is sent to it.
    """
    if isinstance(endpoint, str) and endpoint.strip() == DECISION_PROVIDER_ENDPOINT_DEFAULT:
        return PRESET_JEV
    if isinstance(endpoint, str) and endpoint.strip() == ENDPOINT_NONE:
        return PRESET_NONE
    if _is_route_built(endpoint):
        for m in LOCAL_MODELS:
            if model == m.model:
                return m.id
    return "custom"


def as_payload(m: LocalModel) -> dict:
    """*m* as the JSON the dashboard reads: the numbers a reader chooses by."""
    payload = asdict(m)
    for internal in ("files", "launcher", "requirements", "revision"):
        payload.pop(internal)
    payload["download_bytes"] = m.download_bytes
    payload["recommended_total_ram_gb"] = m.recommended_total_ram_gb
    return payload
