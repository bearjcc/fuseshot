"""Pinned downloads: the browser viewer libraries and the model files.

Nothing here is shipped with Fuseshot. Each file is fetched the first time it
is needed (or with `fuseshot fetch-assets`) from a fixed URL, checked against
the SHA-256 recorded below, and kept in the home folder. A file that does not
match is never used.

A model can also be pointed at a file you already have with the [models] table
of the settings file, e.g. `big-lama = 'D:/models/big-lama.pt'`. Such a file is
used as it is, without a checksum.
"""

import base64
import hashlib
import io
import os
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import config, log

LOG = log.get(__name__)

OPENCV_ZOO = "https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models"
MODELS = {
    "yunet": {
        "file": "face_detection_yunet_2023mar.onnx",
        "url": f"{OPENCV_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "size": 232589, "licence": "MIT", "for": "group",
    },
    "sface": {
        "file": "face_recognition_sface_2021dec.onnx",
        "url": f"{OPENCV_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "size": 38696353, "licence": "Apache-2.0", "for": "group",
    },
    "face-landmarker": {
        "file": "face_landmarker.task",
        "url": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
               "face_landmarker.task",
        "sha256": "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
        "size": 3758596, "licence": "Apache-2.0", "for": "group",
    },
    "efficientdet-lite2": {
        "file": "efficientdet_lite2.tflite",
        "url": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite2/float32/1/"
               "efficientdet_lite2.tflite",
        "sha256": "ad2abbf2b4e10585e15176fd7b5ef03c28dda959ae26fc142549fdd1814db91d",
        "size": 23096891, "licence": "Apache-2.0", "for": "group",
    },
    "big-lama": {
        "file": "big-lama.pt",
        "url": "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt",
        "sha256": "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9",
        "size": 205669692, "licence": "Apache-2.0", "for": "fill",
    },
    "realesrgan-x4plus": {
        "file": "RealESRGAN_x4plus.pth",
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
        "sha256": "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1",
        "size": 67040989, "licence": "BSD-3-Clause", "for": "enhance",
    },
    "scunet-real-psnr": {
        "file": "scunet_color_real_psnr.pth",
        "url": "https://github.com/cszn/KAIR/releases/download/v1.0/scunet_color_real_psnr.pth",
        "sha256": "fa78899ba2caec9d235a900e91d96c689da71c42029230c2028b00f09f809c2e",
        "size": 71982841, "licence": "Apache-2.0", "for": "enhance",
    },
    "fbcnn-color": {
        "file": "fbcnn_color.pth",
        "url": "https://github.com/jiaxi-jiang/FBCNN/releases/download/v1.0/fbcnn_color.pth",
        "sha256": "8b0e4ef23d59cf7ac934a342cb31a17619e4fa4a0b3374a9d78c5174312387e8",
        "size": 287755111, "licence": "Apache-2.0", "for": "enhance",
    },
    "nafnet-gopro-width64": {
        # The authors publish this on Google Drive only; this mirror's file has the same SHA-256.
        "file": "NAFNet-GoPro-width64.pth",
        "url": "https://huggingface.co/nyanko7/nafnet-models/resolve/main/NAFNet-GoPro-width64.pth",
        "sha256": "329d3ab4077b8d6b7ff61de376e483714667960bf85be027bf4335cda701196f",
        "size": 271778961, "licence": "MIT", "for": "enhance",
    },
    # Optional packs: downloaded only after the licence is agreed to in Settings (packs.py).
    "codeformer": {
        "file": "codeformer.pth",
        "url": "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/codeformer.pth",
        "sha256": "1009e537e0c2a07d4cabce6355f53cb66767cd4b4297ec7a4a64ca4b8a5684b7",
        "size": 376637898, "licence": "S-Lab-1.0", "for": "face",
    },
    "ddcolor-modelscope": {
        "file": "ddcolor_modelscope.pth",
        "url": "https://huggingface.co/piddnad/DDColor-models/resolve/main/ddcolor_modelscope.pth",
        "sha256": "17c460d7e55b32a598370621d77173be59e03c24b0823f06821db23a50c263ce",
        "size": 911950059, "licence": "Apache-2.0", "for": "colourise",
    },
}

NPM = "https://registry.npmjs.org"
VIEWER = {
    "three": {
        "version": "0.186.1",
        "tarball": f"{NPM}/three/-/three-0.186.1.tgz",
        "integrity": "sha512-blFeqb49wRCSGUGj7gtpfnSGHy2lwDk94RhUmS1c/hTby70kvChbWpkJ4Pm1390LqzzvTmzgXKHPEafJwCb8jA==",
        "files": {
            "three/three.module.js": ("build/three.module.js",
                                      "9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea"),
            "three/three.core.js": ("build/three.core.js",
                                    "9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6"),
            "three/addons/controls/OrbitControls.js": (
                "examples/jsm/controls/OrbitControls.js",
                "3d79d07ecb686b4e5d93232eedab255331c1beef711e13164eaa1f68655a5f2b"),
            "three/addons/postprocessing/Pass.js": (
                "examples/jsm/postprocessing/Pass.js",
                "444b409c235ead986893c472e720da1b779a56985c7d10b279c7944b52bd61c5"),
            "three/LICENSE": ("LICENSE", "8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc"),
        },
    },
    "@sparkjsdev/spark": {
        "version": "2.4.0",
        "tarball": f"{NPM}/@sparkjsdev/spark/-/spark-2.4.0.tgz",
        "integrity": "sha512-aUjgBfhwDRcF3nUwiAD6Nh7IwvxEmW9liz2qrAXIm2BlPYBmn81QbXIpGFLBSr/o0TKGvEXbNIsKujgPGV/7QQ==",
        "files": {
            "spark/spark.module.min.js": ("dist/spark.module.min.js",
                                          "a2f01f68dbcd5e060d55348f803e509fc789b97904e01b98763fcf0314351aaf"),
            "spark/LICENSE": ("LICENSE", "51829693e5dccd9ca1daa093991faac3aaa93238eb8fd5f5cb4130af85791d64"),
        },
    },
    "@photo-sphere-viewer/core": {
        "version": "5.15.1",
        "tarball": f"{NPM}/@photo-sphere-viewer/core/-/core-5.15.1.tgz",
        "integrity": "sha512-nhJviczUB1xiAT/I7mGba9iDIw7DvsiK34hbR9t7kIrg9MySG33uA+a4QUk2dEOP5PjcKMj58wod8I/dQ7ePiw==",
        "files": {
            "psv/index.module.js": ("index.module.js",
                                    "e5e821b266aa79ebef6491dbb8596398a4a1e32cc49b1bd366cabacb6b0a24e7"),
            "psv/index.css": ("index.css", "de908f4ada0be8211a68303179254c2dcb7e28701973cb813f493dd70d0c33ba"),
            "psv/LICENSE": ("LICENSE", "77aa2ccdd22d14998e9982210b48938a34be2806ad624ef04fe6b6424f5df14d"),
        },
    },
}


class AssetError(RuntimeError):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "fuseshot"})
    return urllib.request.urlopen(request, timeout=60)


def _place(data_or_temp, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data_or_temp, bytes):
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".")
        with os.fdopen(fd, "wb") as f:
            f.write(data_or_temp)
        data_or_temp = tmp
    os.replace(data_or_temp, dest)


# Models

def models_dir():
    return config.path("models")


def model_override(name):
    value = config.section("models").get(name)
    return Path(value).expanduser() if value else None


def model_path(name):
    return model_override(name) or models_dir() / MODELS[name]["file"]


def model(name, fetch=True):
    """Path to a model file, downloading and checking it first if needed."""
    override = model_override(name)
    if override:
        if not override.is_file():
            raise AssetError(f"the [models] setting for {name} points at {override}, which does not exist")
        return override
    path = model_path(name)
    if not path.is_file():
        if not fetch:
            raise AssetError(f"model {name} is missing; run `fuseshot fetch-assets`")
        download_model(name)
    return path


def download_model(name):
    spec = MODELS[name]
    dest = models_dir() / spec["file"]
    LOG.info("downloading %s (%.0f MB) from %s", name, spec["size"] / 1e6, spec["url"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".")
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as f, _get(spec["url"]) as response:
            for block in iter(lambda: response.read(1 << 20), b""):
                h.update(block)
                f.write(block)
        if h.hexdigest() != spec["sha256"]:
            raise AssetError(f"{name} download did not match its checksum; not using it")
        _place(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def check_model(name):
    """(state, path): ok, custom, missing or mismatch."""
    path = model_path(name)
    if not path.is_file():
        return "missing", path
    if model_override(name):
        return "custom", path
    return ("ok" if sha256_file(path) == MODELS[name]["sha256"] else "mismatch"), path


# Viewer libraries

def viewer_dir():
    return config.path("tools", "viewer")


def viewer_files():
    return {dest: (pkg, src, digest) for pkg, spec in VIEWER.items()
            for dest, (src, digest) in spec["files"].items()}


def viewer_ready():
    root = viewer_dir()
    return all((root / dest).is_file() for dest in viewer_files())


def fetch_viewer(force=False):
    root = viewer_dir()
    for pkg, spec in VIEWER.items():
        wanted = spec["files"]
        if not force and all((root / dest).is_file() and sha256_file(root / dest) == digest
                             for dest, (_, digest) in wanted.items()):
            continue
        LOG.info("downloading %s %s from npm", pkg, spec["version"])
        with _get(spec["tarball"]) as response:
            data = response.read()
        algo, expected = spec["integrity"].split("-", 1)
        if base64.b64encode(hashlib.new(algo, data).digest()).decode() != expected:
            raise AssetError(f"{pkg} {spec['version']} did not match its npm integrity hash")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for dest, (src, digest) in wanted.items():
                member = tar.extractfile("package/" + src)
                if member is None:
                    raise AssetError(f"{pkg} has no {src}")
                content = member.read()
                if hashlib.sha256(content).hexdigest() != digest:
                    raise AssetError(f"{pkg}/{src} did not match its checksum")
                _place(content, root / dest)
    versions = "".join(f"{pkg} {spec['version']} (MIT) {spec['tarball']}\n" for pkg, spec in VIEWER.items())
    _place(versions.encode(), root / "VERSIONS.txt")
    return root


def fetch_all(groups=("viewer", "group", "fill")):
    if "viewer" in groups:
        fetch_viewer()
    for name, spec in MODELS.items():
        if spec["for"] in groups and check_model(name)[0] in ("missing", "mismatch"):
            download_model(name)

