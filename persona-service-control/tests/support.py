"""Load the standalone controller without installing it."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_control():
    spec = importlib.util.spec_from_file_location(
        "persona_service_control",
        Path(__file__).resolve().parents[1] / "persona_service_control.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def example_data():
    return {
        "version": 1,
        "id": "example-browser",
        "account": "pb-example",
        "uid": 12345,
        "unit": "chrome-example.service",
    }


def example_lookup(account):
    if account != "pb-example":
        raise KeyError(account)
    return SimpleNamespace(pw_uid=12345, pw_gid=23456)
