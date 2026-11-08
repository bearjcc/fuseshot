"""Optional model packs, installed only when asked for (the way Audacity offers its
optional libraries).

Fuseshot never ships these models and never fetches them on its own. Settings shows
each pack's licence in plain words with a link to the full text; only after Agree is
the file downloaded into the models folder and checked against its SHA-256. The
agreement is kept beside it in <pack>.agreed.json. Both packs load through
spandrel_extra_arches, which has to be added to the Python that runs the GPU jobs.
"""

import json
import threading
from datetime import datetime, timezone

from . import assets, jobs
from .files import write_atomic

PACKS = {
    "face": {
        "title": "Face repair",
        "button": "Install face repair",
        "model": "codeformer",
        "what": "CodeFormer redraws faces that came out wrong after restoring. It is used only when "
                "you mark a result with Face looks wrong.",
        "licence": "S-Lab License 1.0",
        "plain": "Free for non-commercial use only: you may not use the model, or the photos it makes, "
                 "to earn money. If you share the model, its licence goes with it. Fuseshot does not "
                 "include it; it comes straight from its authors.",
        "link": "https://github.com/sczhou/CodeFormer/blob/master/LICENSE",
    },
    "colourise": {
        "title": "Colourising",
        "button": "Install colourising",
        "model": "ddcolor-modelscope",
        "what": "DDColor adds colour to black-and-white scans. It is suggested only when Suggest "
                "colourising is on.",
        "licence": "Apache License 2.0",
        "plain": "Free to use, also for work, and to share. If you share the model, keep its licence "
                 "and notices with it. The colours are invented by the model, so they may be wrong.",
        "link": "https://github.com/piddnad/DDColor/blob/master/LICENSE",
    },
}
EXTRA_MODULE = "spandrel_extra_arches"
_installing = {}
_lock = threading.Lock()


def agreement_file(pack):
    return assets.models_dir() / f"{pack}.agreed.json"


def installed(pack):
    """Agreed to and downloaded (or pointed at a file of one's own under [models])."""
    spec = PACKS[pack]
    if assets.model_override(spec["model"]):
        return assets.model_override(spec["model"]).is_file()
    return agreement_file(pack).is_file() and assets.model_path(spec["model"]).is_file()


def status():
    from .doctor import _python_has

    has_extra = _python_has(jobs.python_for("fill"), EXTRA_MODULE)
    found = {}
    for name, spec in PACKS.items():
        model = assets.MODELS[spec["model"]]
        found[name] = {**{k: spec[k] for k in ("title", "button", "what", "licence", "plain", "link")},
                       "size_mb": round(model["size"] / 1e6), "installed": installed(name),
                       "installing": name in _installing, "error": _installing.get(name) or None,
                       "needs": None if has_extra else needs_hint()}
    return found


def needs_hint():
    python = jobs.python_for("fill")
    python = f'"{python}"' if " " in str(python) else python
    return f"{python} -m pip install {EXTRA_MODULE}"


def install(pack, agree):
    """Download a pack once its licence is agreed to. Blocks until the file is checked."""
    if pack not in PACKS:
        raise ValueError(f"no pack called {pack!r}")
    if agree is not True:
        raise ValueError("the licence has to be agreed to first")
    spec = PACKS[pack]
    with _lock:
        if pack in _installing:
            raise ValueError(f"{spec['title']} is already downloading")
        _installing[pack] = ""
    try:
        try:
            assets.model(spec["model"])
        except (assets.AssetError, OSError) as err:
            raise ValueError(f"could not download {spec['title']}: {err}") from None
        record = {"pack": pack, "model": spec["model"], "licence": spec["licence"], "link": spec["link"],
                  "sha256": assets.MODELS[spec["model"]]["sha256"],
                  "agreed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        write_atomic(agreement_file(pack), json.dumps(record, indent=2))
    finally:
        with _lock:
            _installing.pop(pack, None)
    return status()[pack]
