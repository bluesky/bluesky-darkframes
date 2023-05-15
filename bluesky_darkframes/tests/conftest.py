import pytest
from bluesky.tests.conftest import RE as RE_bluesky  # noqa


@pytest.fixture(scope="function", params=[False])  # call_returns_result=False
def RE(RE_bluesky):  # noqa F811
    return RE
