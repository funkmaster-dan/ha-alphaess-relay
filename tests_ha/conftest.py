"""Real HA fixtures, with all TCP connections restricted to literal loopback."""
from pathlib import Path
import os
import sys

import pytest
import pytest_socket

COMPONENT_ROOT = Path(os.environ.get("ALPHAESS_COMPONENT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(COMPONENT_ROOT))


@pytest.fixture(autouse=True)
def local_only(socket_enabled, enable_custom_integrations):
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    import custom_components.alphaess_relay as component
    assert Path(component.__file__).resolve().is_relative_to(COMPONENT_ROOT.resolve())
