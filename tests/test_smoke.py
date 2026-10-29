import sys

import cdse_stac


def test_python_is_new_enough():
    assert sys.version_info >= (3, 11)


def test_package_imports():
    assert cdse_stac.__version__ == "0.1.0"
