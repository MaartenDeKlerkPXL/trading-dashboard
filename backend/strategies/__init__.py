"""Strategy registry: every `Strategy` subclass in this package, keyed by "name@version"."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from .base import Strategy

_EXCLUDED = {"base", "indicators"}


def load_strategies() -> dict[str, type[Strategy]]:
    found: dict[str, type[Strategy]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name in _EXCLUDED or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, Strategy) and cls is not Strategy and cls.__module__ == module.__name__:
                key = cls.key()
                if key in found:
                    raise RuntimeError(f"Strategy {key} is defined twice")
                found[key] = cls
    return dict(sorted(found.items()))


def describe(cls: type[Strategy]) -> dict:
    return {
        "key": cls.key(),
        "name": cls.name,
        "version": cls.version,
        "label": cls.label,
        "description": cls.description,
        "code_hash": code_hash(cls),
        "params": [
            {"name": p.name, "label": p.label, "type": p.kind, "default": p.default,
             "min": p.min, "max": p.max, "step": p.step, "help": p.help}
            for p in cls.params
        ],
    }


def code_hash(cls: type[Strategy]) -> str:
    """Short fingerprint of the strategy's source file, to detect silent edits of a version."""
    import hashlib

    source = inspect.getsource(inspect.getmodule(cls))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
