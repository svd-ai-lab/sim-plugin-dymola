"""Protocol-conformance test for the Dymola plugin."""
from __future__ import annotations

from sim.testing import assert_protocol_conformance
from sim_plugin_dymola import DymolaDriver


def test_protocol_conformance() -> None:
    assert_protocol_conformance(DymolaDriver)

