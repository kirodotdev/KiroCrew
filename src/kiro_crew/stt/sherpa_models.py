"""The sherpa-onnx model catalog, and a sha256-pinned downloader for it.

A sherpa-onnx transducer is four files (encoder, decoder, joiner, token table),
published by k2-fsa as one ``.tar.bz2`` release asset. The archive is pinned by its
sha256 and each file inside it by its own size and sha256, so what lands on disk is
exactly the four pinned files and nothing else from the archive.

The trust doctrine is the whisper store's (:mod:`kiro_crew.stt.models`): the
network fetch reuses :func:`models.stream_pinned_payload`, the files live under the
sealed read-only ``<data home>/models`` leaf, and every load re-hashes them through
the same one-descriptor check, so a same-size file dropped over the weights is
refused rather than trusted.

No numpy and no ``sherpa_onnx`` here: the config loader imports this module for the
catalog names on the boot path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.stt import models as whisper_models

logger = logging.getLogger(__name__)

#: The k2-fsa release that carries every sherpa-onnx ASR model archive.
SHERPA_MODELS_BASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

#: Operator override for the base URL, for an air-gapped or mirrored install. The
#: pins still apply, so a mirror can serve the bytes but cannot substitute them.
MODEL_URL_ENV = "KIROCREW_SHERPA_MODEL_BASE_URL"

#: The same switch the whisper and embedding downloaders honour.
SKIP_DOWNLOAD_ENV = whisper_models.SKIP_DOWNLOAD_ENV

_CHUNK_BYTES = 1 << 20
_STAGING_SUFFIX = ".part"

#: The store's status ``error`` when this Python cannot read the archive.
BZ2_MISSING_ERROR = "this Python has no bz2 module, so it cannot unpack the model archive"

#: What the Vietnamese rows are trained from, and the terms their weights carry.
_ZIPFORMER_VI_SOURCE = "https://huggingface.co/hynt/Zipformer-30M-RNNT-6000h"
_ZIPFORMER_VI_LICENSE = "CC-BY-NC-ND-4.0"


@dataclass(frozen=True)
class SherpaFile:
    """One file the recogniser loads: where it sits in the archive, and its pin."""

    #: Path of the member inside the archive.
    member: str
    size_bytes: int
    sha256: str

    @property
    def filename(self) -> str:
        """The name it is stored under in the model's directory."""
        return self.member.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class SherpaModel:
    """One catalog row: an offline transducer and the archive it ships in."""

    name: str
    #: ISO 639-1 codes of the languages the model recognises; empty when unknown.
    languages: tuple[str, ...]
    archive: str
    archive_size_bytes: int
    archive_sha256: str
    encoder: SherpaFile
    decoder: SherpaFile
    joiner: SherpaFile
    tokens: SherpaFile
    #: SPDX identifier of the weights' licence as the source states it, shown before
    #: the download; ``""`` when the source states none.
    license: str
    source_url: str
    #: Directory under :func:`models_dir`; the name when empty.
    directory: str = ""

    @property
    def files(self) -> tuple[SherpaFile, ...]:
        return (self.encoder, self.decoder, self.joiner, self.tokens)

    @property
    def size_bytes(self) -> int:
        """What the model occupies on disk once installed."""
        return sum(f.size_bytes for f in self.files)


def _vi_row(name: str, archive: str, size: int, sha256: str, *, int8: bool) -> SherpaModel:
    """One of the two Vietnamese 30M rows, which differ only in the encoder and joiner."""
    prefix = archive.removesuffix(".tar.bz2")
    if int8:
        encoder = SherpaFile(
            f"{prefix}/encoder.int8.onnx",
            27699063,
            "8ef5286dd427eb108055c2ddc1982aa31e544706072d5ea228729292dacade68",
        )
        joiner = SherpaFile(
            f"{prefix}/joiner.int8.onnx",
            1033417,
            "7311d2e17b810ecea515d79c71cc4668af8759256a06fa01d27047772320c821",
        )
    else:
        encoder = SherpaFile(
            f"{prefix}/encoder.onnx",
            92184132,
            "b0daa9842a1f39d146e57d6e951edc8910ddd234cbb00e9b5015a5280a5ba221",
        )
        joiner = SherpaFile(
            f"{prefix}/joiner.onnx",
            4104465,
            "d861afe55f7ff43c90069cad0a5d07261a408be5c7fd2aac8c84b1f3225da021",
        )
    return SherpaModel(
        name=name,
        languages=("vi",),
        archive=archive,
        archive_size_bytes=size,
        archive_sha256=sha256,
        encoder=encoder,
        # The decoder is fp32 in both archives.
        decoder=SherpaFile(
            f"{prefix}/decoder.onnx",
            5165084,
            "cf2aa385b82c9d5d40cd29c3188af52d0249b3b78f0d4b7eb84ad502d50c7e7f",
        ),
        joiner=joiner,
        tokens=SherpaFile(
            f"{prefix}/tokens.txt",
            23238,
            "ca8171f8bbd516c050b627582f2125c8f5f1f6ed967ab41b0fa9aae2cf61b492",
        ),
        license=_ZIPFORMER_VI_LICENSE,
        source_url=_ZIPFORMER_VI_SOURCE,
    )


#: The order the picker offers: the default first, then by language. Every digest was
#: taken by downloading the archive and hashing it and each extracted member, and each
#: model decoded the test recording its archive ships with; an archive digest GitHub
#: publishes for the release asset matched too. ``license`` is what the source model
#: card states, ``""`` where it states none.
CATALOG: tuple[SherpaModel, ...] = (
    _vi_row(
        "zipformer-vi-30m-int8",
        "sherpa-onnx-zipformer-vi-30M-int8-2026-02-09.tar.bz2",
        26442384,
        "da8b637947091829d7ee9eda23da2a4ec7caa399233a3f4e34eb719fb2ea6b9b",
        int8=True,
    ),
    _vi_row(
        "zipformer-vi-30m",
        "sherpa-onnx-zipformer-vi-30M-2026-02-09.tar.bz2",
        95081562,
        "975c40bc71074958cc6d24acb11aeb1958f9376a87c0fa3c369cb884e203e20a",
        int8=False,
    ),
    SherpaModel(
        name="zipformer-en-small",
        languages=("en",),
        archive="sherpa-onnx-zipformer-small-en-2023-06-26.tar.bz2",
        archive_size_bytes=112232184,
        archive_sha256="c8bff1091c26c49731cddbcd60ef18061142ea11523df1b73bf1b14451b9c15e",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-small-en-2023-06-26/encoder-epoch-99-avg-1.int8.onnx",
            26015366,
            "3a6ac78a31cc2c60ca8c1e2e2f43c878fbbcaf051ada4900e0a42ef8ba53d375",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-small-en-2023-06-26/decoder-epoch-99-avg-1.onnx",
            2092621,
            "1621a13eaa7f6fc52380f025ee2a6a05b56a87754ab5a0f4f580a3928f3467a2",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-small-en-2023-06-26/joiner-epoch-99-avg-1.int8.onnx",
            259335,
            "6b183b6ec656e4d3ca6b86d0aeca992dac14df80aae6b416d7c068d0ff2bd4d7",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-small-en-2023-06-26/tokens.txt",
            5048,
            "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb",
        ),
        license="Apache-2.0",
        source_url="https://huggingface.co/csukuangfj/sherpa-onnx-zipformer-small-en-2023-06-26",
    ),
    SherpaModel(
        name="zipformer-en-gigaspeech",
        languages=("en",),
        archive="sherpa-onnx-zipformer-gigaspeech-2023-12-12.tar.bz2",
        archive_size_bytes=310494228,
        archive_sha256="2eebc47bcb4d13a0481f32c60152b76223b1e6ae8a1cb16dd798b60ec7086be2",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-gigaspeech-2023-12-12/encoder-epoch-30-avg-1.int8.onnx",
            72850738,
            "d3eff4b1bd747bd781a47966795988539227d05638524b0313f26d3a166962d7",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-gigaspeech-2023-12-12/decoder-epoch-30-avg-1.onnx",
            2093081,
            "852b65653e65ec5436b61ab8cea819847666c1dd4c5821a94989788f85adbe76",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-gigaspeech-2023-12-12/joiner-epoch-30-avg-1.int8.onnx",
            259417,
            "80160e45cca71dd52f6b0a6d3d12be18126f5308b2d4ba03f001300fea377c64",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-gigaspeech-2023-12-12/tokens.txt",
            5020,
            "0ef7d736bf4de3ef947292e4b119ef13f6808cd5f3aec225a843a7135ac1c2ce",
        ),
        license="Apache-2.0",
        source_url="https://huggingface.co/yfyeung/icefall-asr-gigaspeech-zipformer-2023-10-17",
    ),
    SherpaModel(
        name="zipformer-en-libriheavy-punct",
        languages=("en",),
        archive="sherpa-onnx-zipformer-en-libriheavy-20230830-small-punct-case.tar.bz2",
        archive_size_bytes=309964940,
        archive_sha256="0547a489e4077a0de360c9999b2e3f8c920e0f608acf44101aee45def0463642",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-en-libriheavy-20230830-small-punct-case/encoder-epoch-88-avg-41.int8.onnx",
            68780141,
            "2922ebd6b47954a02cc7e45e1ffb81a78d269b9949c86c41e7267f0efb995147",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-en-libriheavy-20230830-small-punct-case/decoder-epoch-88-avg-41.onnx",
            2616855,
            "ac5c45e8cbeb4d7ecd85972be8c00a1553f84a0d067700e84ce72d7beb711747",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-en-libriheavy-20230830-small-punct-case/joiner-epoch-88-avg-41.int8.onnx",
            391431,
            "4aa9d1dd5019e7444cfb1aa5c56889f4de3e657156e113980ad40c6d73ddee77",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-en-libriheavy-20230830-small-punct-case/tokens.txt",
            7368,
            "8eb972ab97543df33e055e10934fc48fd827116760ea11c7ed29214e0dec4e3b",
        ),
        license="",
        source_url="https://huggingface.co/csukuangfj/sherpa-onnx-zipformer-en-libriheavy-20230830-small-punct-case",
    ),
    SherpaModel(
        name="zipformer-zh",
        languages=("zh",),
        archive="sherpa-onnx-zipformer-multi-zh-hans-2023-9-2.tar.bz2",
        archive_size_bytes=309195644,
        archive_sha256="c4925a6b0f998800d16f80caf90d2decff7b7a8c156d044c6cffdf141c847d94",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-multi-zh-hans-2023-9-2/encoder-epoch-20-avg-1.int8.onnx",
            69037476,
            "6dd1748110f55cb7e9ce00a6abd4b96959fa3f2a25a20d0c52184dd1eb8c9ece",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-multi-zh-hans-2023-9-2/decoder-epoch-20-avg-1.onnx",
            5165142,
            "f6bbd5fc9cfa2c69988fafb63e5ccd50756134993eb6c23d89887d81e7f7896c",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-multi-zh-hans-2023-9-2/joiner-epoch-20-avg-1.int8.onnx",
            1033479,
            "fc04f95a7c184390f48a64eab541f7bcab7c856b5df03bf67f227d1c74b49f99",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-multi-zh-hans-2023-9-2/tokens.txt",
            18610,
            "67352babdf908352d3ce19c4496cb05ca173805616c23b520927117b2c2e2ffa",
        ),
        license="",
        source_url="https://huggingface.co/zrjin/icefall-asr-multi-zh-hans-zipformer-2023-9-2",
    ),
    SherpaModel(
        name="zipformer-zh-en",
        languages=("zh", "en"),
        archive="sherpa-onnx-zipformer-zh-en-2023-11-22.tar.bz2",
        archive_size_bytes=312398028,
        archive_sha256="0c3f2b9c884335a6931b8ccee6ede30e8dd3f89efc289ff64cd79d530a3bcf91",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-zh-en-2023-11-22/encoder-epoch-34-avg-19.int8.onnx",
            69037476,
            "28780a73b0fc5a05ec579ad4dbbc7d8fcd09fed5e6c431f84dcaab8f8170d9bf",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-zh-en-2023-11-22/decoder-epoch-34-avg-19.onnx",
            5165101,
            "110dc19143f0731ce0f64513e524d7d8f729e37107e1392da790f66d6c936a0b",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-zh-en-2023-11-22/joiner-epoch-34-avg-19.int8.onnx",
            1033479,
            "498861ff0963ecbbff101a940ed533a715e5d68ee96431a3b092d0ca00c3ba38",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-zh-en-2023-11-22/tokens.txt",
            25645,
            "2d5d6245197582cea396b23fa29d592705be31bac9a97bb74a8d1bca3ca6a0e0",
        ),
        license="",
        source_url="https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/zipformer-transducer-models.html",
    ),
    SherpaModel(
        name="zipformer-zh-en-punct-int8",
        languages=("zh", "en"),
        archive="sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03.tar.bz2",
        archive_size_bytes=136396739,
        archive_sha256="5d02c36d7b44e886b7c8f0d8e051f8713acab96c264bb6ef9e718be39a6a2224",
        encoder=SherpaFile(
            "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03/encoder-epoch-99-avg-1.int8.onnx",
            161015713,
            "7f6aa62056efd8af9da13e0faa81cd3f284d2fb2e3b63de56fd2dfd3450910dc",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03/decoder-epoch-99-avg-1.onnx",
            11309084,
            "72f47405d3c1033bebccbef82f90071e7b4ba3e71b9c986f2b74244b25723aed",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03/joiner-epoch-99-avg-1.int8.onnx",
            2581422,
            "aedb7fa697b2ab43f20499826fff7c997eea7d67db77be97769aeeeb726e63b3",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03/tokens.txt",
            58806,
            "b818a60878b9aae978cbb8ad594acbd403d76d1af2e31ef4197c84e2dbdba27c",
        ),
        license="Apache-2.0",
        source_url="https://github.com/Gilgamesh-J/X-ASR",
    ),
    SherpaModel(
        name="zipformer-yue",
        languages=("yue",),
        archive="sherpa-onnx-zipformer-cantonese-2024-03-13.tar.bz2",
        archive_size_bytes=329964914,
        archive_sha256="857277b80147b96e7e992fc38a88ceac6da5ad728aad966b131f993b822256b9",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-cantonese-2024-03-13/encoder-epoch-45-avg-35.int8.onnx",
            69285978,
            "8bf7a4618dd5622b68b18444768af590c7a958e385ed2b60836ea166964c4a98",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-cantonese-2024-03-13/decoder-epoch-45-avg-35.onnx",
            10989238,
            "4168bd9ede7f27a7e6725032dd6f384f675ca79a8eb3a1806231e630a8c3b6ed",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-cantonese-2024-03-13/joiner-epoch-45-avg-35.int8.onnx",
            2504924,
            "3886aa27beee1a845889453352786cc33f809a84e36c522e4b4e6b3492e90ee0",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-cantonese-2024-03-13/tokens.txt",
            42525,
            "0f5545212b7042e8bca55e0b06295330dd19ec21f04c446e8a656ce7c930eee4",
        ),
        license="",
        source_url="https://huggingface.co/zrjin/icefall-asr-mdcc-zipformer-2024-03-11",
    ),
    SherpaModel(
        name="zipformer-ja",
        languages=("ja",),
        archive="sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01.tar.bz2",
        archive_size_bytes=713097333,
        archive_sha256="e0981d0d5d7b446d41010831b59091ebf57d2aa7b79980f67ca37af460b5842d",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01/encoder-epoch-99-avg-1.int8.onnx",
            154670139,
            "2c7bd08a8a99f9ddd0d9e458456577b1f6279214e51426f114f9eced44c54e1d",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01/decoder-epoch-99-avg-1.onnx",
            11767836,
            "58b18211ae06265466bfa17172dab574df94f76c8bcb61a3640c28ba860e4124",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01/joiner-epoch-99-avg-1.int8.onnx",
            2696970,
            "49cc7ea1d3d35a40a27442db5e89996da64bf0e683a903dce76e99e57a12e4de",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01/tokens.txt",
            45754,
            "2c3ac659818a48a0c04010e0593bbc4d7c8a24a054340b01131499c05fd52def",
        ),
        license="Apache-2.0",
        source_url="https://huggingface.co/reazon-research/reazonspeech-k2-v2",
    ),
    SherpaModel(
        name="zipformer-ko",
        languages=("ko",),
        archive="sherpa-onnx-zipformer-korean-2024-06-24.tar.bz2",
        archive_size_bytes=329740690,
        archive_sha256="24bd409318f389cd2de0e295eb1acf91f4e8dfcc0d650490dd2a01f5b50d2c77",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-korean-2024-06-24/encoder-epoch-99-avg-1.int8.onnx",
            70784728,
            "8b196d723421a0513c98ec25da2c43420c029e817f5e4a90b29ff80291c0af2b",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-korean-2024-06-24/decoder-epoch-99-avg-1.onnx",
            11309084,
            "b486221625f4680659c21eaf0505c50e64a897583af4f9ca6f87dea762a33885",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-korean-2024-06-24/joiner-epoch-99-avg-1.int8.onnx",
            2581421,
            "eb654db1ea2cc9d63474855f65958b6059084692a9f2eb4f3812aceb1e416a20",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-korean-2024-06-24/tokens.txt",
            60246,
            "016bdf0965029263b7ad01b742366ee542ef0bef38261510e8176ff6f2e9e668",
        ),
        license="Apache-2.0",
        source_url="https://huggingface.co/k2-fsa/sherpa-onnx-zipformer-korean-2024-06-24",
    ),
    SherpaModel(
        name="zipformer-ru-int8",
        languages=("ru",),
        archive="sherpa-onnx-zipformer-ru-int8-2025-04-20.tar.bz2",
        archive_size_bytes=60239942,
        archive_sha256="d6a651569aacc9a177259fa54705dd76acae23f6a4d62ea6797bd220d4b57163",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-ru-int8-2025-04-20/encoder.int8.onnx",
            70876638,
            "eb6c12fbad810d5bc3e427802e604604c69b5943a91feebc43424dd09d9ec407",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-ru-int8-2025-04-20/decoder.onnx",
            2093080,
            "dcbe1ffa0211e77ca6d3a80164df13fbda3ec00e47d12b9f449f89572df12136",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-ru-int8-2025-04-20/joiner.int8.onnx",
            259417,
            "93f2e1d12b78d53e7802f1606488c14bb3d764b15fadf5ef6c022f6ba1fa40f7",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-ru-int8-2025-04-20/tokens.txt",
            6388,
            "93bbbc0bae6b78c0bbb743d4aa9fded3bb5ff3aac5f0200e3a769a5a05e0fdf6",
        ),
        license="Apache-2.0",
        source_url="https://huggingface.co/alphacep/vosk-model-ru",
    ),
    SherpaModel(
        name="zipformer-th",
        languages=("th",),
        archive="sherpa-onnx-zipformer-thai-2024-06-20.tar.bz2",
        archive_size_bytes=695605000,
        archive_sha256="48819951fc31dde9339d44b7a91c60be8e74f928f9959066a0a0827f400049da",
        encoder=SherpaFile(
            "sherpa-onnx-zipformer-thai-2024-06-20/encoder-epoch-12-avg-5.int8.onnx",
            154671320,
            "9ceb14519103b854bafb41b952ac0aaedc037825c9d5aa991a8849f93d8cfc7d",
        ),
        decoder=SherpaFile(
            "sherpa-onnx-zipformer-thai-2024-06-20/decoder-epoch-12-avg-5.onnx",
            5165084,
            "21ebb9302e1c72b352a4d14a4ae2cf0364dc741d1007dded5110e720b6e17ef4",
        ),
        joiner=SherpaFile(
            "sherpa-onnx-zipformer-thai-2024-06-20/joiner-epoch-12-avg-5.int8.onnx",
            1033417,
            "6ee983924b39ed7d5a57a57963daeaaeafcfc2095f0eae5bb0446fbba34842cf",
        ),
        tokens=SherpaFile(
            "sherpa-onnx-zipformer-thai-2024-06-20/tokens.txt",
            39142,
            "dd5fc99e7251a7dff28d459d812607c6dd4a3acfd248bdd8d869a5c251fc44f5",
        ),
        license="Apache-2.0",
        source_url="https://huggingface.co/yfyeung/icefall-asr-gigaspeech2-th-zipformer-2024-06-20",
    ),
)

DEFAULT_MODEL = "zipformer-vi-30m-int8"

_BY_NAME: dict[str, SherpaModel] = {m.name: m for m in CATALOG}

#: Names accepted for ``stt.sherpa_model``.
MODEL_NAMES: tuple[str, ...] = tuple(_BY_NAME)


def canonical_name(value: object) -> str | None:
    """The catalog name *value* selects, or ``None`` when it names no row."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if name in _BY_NAME else None


def resolve(name: object) -> SherpaModel:
    """The catalog row *name* selects, the default for anything else."""
    return _BY_NAME[canonical_name(name) or DEFAULT_MODEL]


def models_dir() -> Path:
    """Where sherpa-onnx models live: beside the whisper weights, under ``models/``."""
    return config_dir() / "models" / "sherpa"


def model_dir(model: SherpaModel) -> Path:
    return models_dir() / (model.directory or model.name)


def file_path(model: SherpaModel, entry: SherpaFile) -> Path:
    return model_dir(model) / entry.filename


def is_present(model: SherpaModel) -> bool:
    """Whether every file of *model* is on disk at its pinned size. A stat, not a hash."""
    for entry in model.files:
        try:
            if file_path(model, entry).stat().st_size != entry.size_bytes:
                return False
        except OSError:
            return False
    return True


def bz2_available() -> bool:
    """Whether this Python can read the ``.tar.bz2`` the models ship in.

    ``bz2`` is a stdlib module backed by the optional ``_bz2`` extension, which a
    Python built on a host with no libbz2 headers lacks; ``tarfile`` then refuses
    the archive only after it was downloaded.
    """
    try:
        import bz2  # noqa: F401
    except ImportError:
        return False
    return True


def _archive_url(model: SherpaModel) -> str:
    base = os.environ.get(MODEL_URL_ENV, "").strip() or SHERPA_MODELS_BASE_URL
    return f"{base.rstrip('/')}/{model.archive}"


ProgressFn = Callable[[int, int], None]


def _extract_member(tar: tarfile.TarFile, entry: SherpaFile, target_dir: Path) -> Path:
    """Write one pinned member to a staging file in *target_dir*; return that file.

    The member must be a regular file of its pinned size, and its bytes must hash
    to its pin, or :class:`models.ModelDownloadError` is raised and nothing is kept.
    Only the member's bytes are read, never its path or mode, so the archive cannot
    place a file anywhere but the staging name this function chose.
    """
    try:
        info = tar.getmember(entry.member)
    except KeyError:
        raise whisper_models.ModelDownloadError(f"{entry.member}: missing from the archive")
    if not info.isreg() or info.size != entry.size_bytes:
        raise whisper_models.ModelDownloadError(f"{entry.member}: not the pinned file")
    source = tar.extractfile(info)
    if source is None:
        raise whisper_models.ModelDownloadError(f"{entry.member}: unreadable in the archive")
    fd, name = tempfile.mkstemp(dir=target_dir, prefix=f"{entry.filename}.", suffix=_STAGING_SUFFIX)
    staged = Path(name)
    try:
        digest = hashlib.sha256()
        written = 0
        with os.fdopen(fd, "wb") as out, source:
            while True:
                chunk = source.read(_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > entry.size_bytes:
                    raise whisper_models.ModelDownloadError(f"{entry.member}: larger than pinned")
                out.write(chunk)
                digest.update(chunk)
        if written != entry.size_bytes or digest.hexdigest() != entry.sha256:
            raise whisper_models.ModelDownloadError(f"{entry.member}: sha256 mismatch")
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _download_blocking(
    model: SherpaModel,
    on_progress: ProgressFn | None = None,
    on_complete: Callable[[], None] | None = None,
) -> Path:
    """Fetch, verify and install *model*; return its directory. Blocks; never on the loop.

    The archive is staged under an unpredictable exclusive name in ``models/sherpa``,
    so a planted symlink cannot redirect the write, and deleted on every exit. Each
    file is renamed into place only after all four verified, so a failure leaves
    whatever was installed before untouched.
    """
    target = model_dir(model)
    target.mkdir(parents=True, exist_ok=True)
    archive_fd, archive_name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{model.archive}.", suffix=_STAGING_SUFFIX
    )
    archive = Path(archive_name)
    staged: list[tuple[Path, Path]] = []
    try:
        with os.fdopen(archive_fd, "wb") as fh:
            whisper_models.stream_pinned_payload(
                _archive_url(model),
                label=model.name,
                expected_size=model.archive_size_bytes,
                expected_sha256=model.archive_sha256,
                write=fh.write,
                on_progress=on_progress,
            )
        # The archive's bytes are the pinned ones, so its decompressed size is known
        # and bounded; only the four named members are read out of it.
        with tarfile.open(archive, mode="r:bz2") as tar:
            # In archive order: a .tar.bz2 cannot seek back without decompressing
            # from the start again, so each earlier member would cost a whole pass.
            offsets = {m.name: m.offset_data for m in tar.getmembers()}
            for entry in sorted(model.files, key=lambda f: offsets.get(f.member, -1)):
                staged.append((_extract_member(tar, entry, target), file_path(model, entry)))
        for source, final in staged:
            replace_with_retry(source, final)
        staged.clear()
        logger.info("Sherpa-ONNX model %s verified and installed at %s", model.name, target)
        if on_complete is not None:
            on_complete()
        return target
    finally:
        for source, _final in staged:
            try:
                source.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove staging file %s", source, exc_info=True)
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staged archive %s", archive, exc_info=True)


def _verify_blocking(model: SherpaModel) -> bool:
    """Whether every installed file of *model* matches its pin. Blocking.

    A file that fails its digest is deleted, so the caller downloads it again. A
    link on the path or a second hard link raises ``SandboxCeilingUnsealable`` from
    the sandbox checks and is left alone: only the operator can remove that name.
    """
    from kiro_crew import sandbox

    models_root = str(config_dir() / "models")
    ok = True
    for entry in model.files:
        path = file_path(model, entry)
        sandbox.require_real_model_path(str(path), models_root=models_root)
        if not path.is_file():
            ok = False
            continue
        if whisper_models._sha256_verified_file(path) == entry.sha256:
            continue
        logger.error(
            "Sherpa-ONNX model file %s does not match its pinned digest; removing it", path
        )
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove the unverified model file %s", path, exc_info=True)
        ok = False
    return ok


class SherpaModelStore:
    """Serialises downloads and exposes their progress. One per process.

    Holds caller-independent state only (which transfer is running), so it is safe
    in a process that serves many sessions.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._status_lock = threading.Lock()
        self.status: dict[str, object] = {
            "step": "idle",
            "model": "",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": "",
        }

    def _set(
        self, *, step: str, model: str, done: int = 0, total: int = 0, error: str = ""
    ) -> None:
        with self._status_lock:
            self.status = {
                "step": step,
                "model": model,
                "downloaded_bytes": done,
                "total_bytes": total,
                "error": error,
            }

    async def _accept_existing(self, model: SherpaModel) -> tuple[Path | None, bool]:
        """``(directory, refused)``: the verified install, or why there is none."""
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        if not await asyncio.to_thread(is_present, model):
            return None, False
        try:
            verified = await asyncio.to_thread(_verify_blocking, model)
        except SandboxCeilingUnsealable as exc:
            logger.error("Refusing to load Sherpa-ONNX model %s: %s", model.name, exc)
            self._set(step="failed", model=model.name, error=str(exc))
            return None, True
        if not verified:
            return None, False
        self._set(step="ready", model=model.name, total=model.size_bytes)
        return model_dir(model), False

    async def ensure(self, model: SherpaModel) -> Path | None:
        """The verified directory of *model*, downloading it when absent; ``None`` if not.

        Never raises for an unavailable model: the reason is in :attr:`status`.
        """
        accepted, refused = await self._accept_existing(model)
        if accepted is not None or refused:
            return accepted
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            self._set(step="skipped", model=model.name, error="model download disabled")
            return None
        if not bz2_available():
            # Refused before the transfer: the archive could not be opened after it.
            self._set(step="failed", model=model.name, error=BZ2_MISSING_ERROR)
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            accepted, refused = await self._accept_existing(model)
            if accepted is not None or refused:
                return accepted
            total = model.archive_size_bytes
            self._set(step="downloading", model=model.name, total=total)
            landed = threading.Event()

            def _progress(done: int, size: int) -> None:
                if not landed.is_set():
                    self._set(step="downloading", model=model.name, done=done, total=size)

            def _completed() -> None:
                landed.set()
                self._set(step="ready", model=model.name, total=model.size_bytes)

            try:
                await asyncio.to_thread(_download_blocking, model, _progress, _completed)
            except asyncio.CancelledError:
                # The worker thread cannot be stopped and may still land the files;
                # `_completed` then reports ready on its own.
                if not landed.is_set():
                    self._set(step="failed", model=model.name, error="download cancelled")
                raise
            except Exception as exc:
                logger.warning("Sherpa-ONNX model %s download failed: %s", model.name, exc)
                self._set(step="failed", model=model.name, error=str(exc))
                return None
            return model_dir(model)


_store: SherpaModelStore | None = None


def store() -> SherpaModelStore:
    """The process-wide sherpa-onnx model store."""
    global _store
    if _store is None:
        _store = SherpaModelStore()
    return _store
