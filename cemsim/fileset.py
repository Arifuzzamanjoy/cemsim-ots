"""Filesets (saved simulator states) as portable, safe JSON.

Pickle was replaced because (1) a pickle of numpy arrays is tied to the numpy
major version that wrote it (numpy 2 pickles reference ``numpy._core`` and cannot
be read by numpy 1.x), and (2) unpickling a shared file can execute arbitrary
code.  Here numpy arrays and scalars are tagged and restored exactly:

    {"__nd__": "<f8", "shape": [30, 11], "data": [...]}

Python floats are written with ``repr`` precision by ``json``, so a
save/load round trip is bit-exact and a restored simulation is deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

FORMAT = "cemsim-fileset/1"


def encode(o):
    if isinstance(o, np.ndarray):
        return {"__nd__": o.dtype.str, "shape": list(o.shape), "data": o.ravel().tolist()}
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, dict):
        return {str(k): encode(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [encode(v) for v in o]
    return o


def decode(o):
    if isinstance(o, dict):
        if "__nd__" in o:
            return np.array(o["data"], dtype=np.dtype(o["__nd__"])).reshape(o["shape"])
        return {k: decode(v) for k, v in o.items()}
    if isinstance(o, list):
        return [decode(v) for v in o]
    return o


def dump(snapshot: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"format": FORMAT, "state": encode(snapshot)}, fh, separators=(",", ":"))
    tmp.replace(path)  # atomic: a crash never leaves a half-written fileset


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("format") != FORMAT:
        raise ValueError(f"{path.name}: not a {FORMAT} file")
    return decode(doc["state"])


def comment(path: Path) -> str:
    try:
        return load(path).get("comment", "")
    except (OSError, ValueError, KeyError):
        return "?"
