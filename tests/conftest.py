"""Fixtures shared by all tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Load custom_components/duke_energy, with an in-memory recorder.

    The integration depends on the recorder, and the recorder has to start
    before `hass`, so it's requested first.
    """
    return
