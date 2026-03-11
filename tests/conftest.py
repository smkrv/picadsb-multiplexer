"""Shared fixtures for picadsb-multiplexer tests."""
import importlib.util
import os
import sys

import pytest

# The main module has a hyphen in its filename, so we need importlib
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "picadsb-multiplexer.py",
)

_spec = importlib.util.spec_from_file_location("picadsb_multiplexer", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Re-export classes so tests can import from conftest
BeastFormat = _mod.BeastFormat
TimestampGenerator = _mod.TimestampGenerator
PicADSBMultiplexer = _mod.PicADSBMultiplexer
