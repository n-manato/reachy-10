import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fleet_cfg():
    from orchestra.config import FleetConfig

    return FleetConfig.load(ROOT / "config" / "fleet.yaml")


@pytest.fixture(scope="session")
def ctx():
    from orchestra.config import Context

    return Context.load(ROOT)


@pytest.fixture(scope="session")
def lib():
    from orchestra.motion.schema import ClipLibrary

    return ClipLibrary(ROOT / "clips")


def has_sdk() -> bool:
    try:
        import reachy_mini  # noqa: F401

        return True
    except ImportError:
        return False


needs_sdk = pytest.mark.skipif(not has_sdk(), reason="Reachy Mini SDK not installed")
