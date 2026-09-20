import logging
from pathlib import Path

import pytest

from labauto.demo import init_demo
from labauto.lab import Lab, LabConfig

logging.getLogger("labauto").setLevel(logging.WARNING)


@pytest.fixture
def demo(tmp_path: Path):
    """A simulated laboratory with a 2-day-old, unverified calibration."""
    info = init_demo(tmp_path / "lab")
    return tmp_path / "lab", info


@pytest.fixture
def lab(demo):
    root, info = demo
    with Lab(LabConfig.load(root / "lab.toml")) as lab:
        yield lab, info
