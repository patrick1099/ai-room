"""Contract smoke tests: run each vendor CLI once for real.

These are deselected by default (``pytest -m 'not contract'`` is the default
addopts).  Run them with ``pytest -m contract`` when a driver's parser or a
vendor's JSON schema changes.  They are the only guarantee that the parsers
still match the real vendor output, so they are deliberately cheap: one very
short question per driver.
"""

from __future__ import annotations

import pytest

from ai_room.drivers.protocol import DriverRequest
from ai_room.drivers.registry import driver_for


@pytest.mark.contract
@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
def test_contract_driver_returns_session_id(agent: str, tmp_path) -> None:
    request = DriverRequest(
        question="reply with exactly: hi",
        cwd=tmp_path,
        timeout=180.0,
    )
    result = driver_for(agent).invoke(request)
    assert result.session_id is not None, f"{agent} did not return a session id"
    assert result.text.strip()
