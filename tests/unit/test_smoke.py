import importlib

import pytest


@pytest.mark.unit
@pytest.mark.parametrize("package", ["api", "worker", "controller", "common"])
def test_package_imports(package):
    assert importlib.import_module(package) is not None
