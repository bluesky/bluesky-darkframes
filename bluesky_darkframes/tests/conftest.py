import pytest
from bluesky.tests.conftest import RE  # noqa F401


@pytest.fixture(name="RE", scope="function", params=[False])  # call_returns_result=False
def _RE(RE):  # noqa F811
    return RE
