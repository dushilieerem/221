# -*- coding: utf-8 -*-
"""P0-11：用户代码静态护栏。"""
from __future__ import annotations

import pytest

from app.services.security.code_guard import UnsafeUserCode, assert_safe_user_code


def test_allows_strategy_style_code() -> None:
    code = "import numpy as np\nimport pandas as pd\nfrom app.services.strategy.vectorized.factor_strategy import TopN\nx = np.array([1])\n"
    assert_safe_user_code(code, kind="策略")


@pytest.mark.parametrize(
    "code",
    [
        "import os\n",
        "import socket\n",
        "open('/etc/passwd').read()\n",
        "eval('1+1')\n",
        "().__class__.__bases__[0].__subclasses__()\n",
    ],
)
def test_rejects_dangerous_code(code: str) -> None:
    with pytest.raises(UnsafeUserCode):
        assert_safe_user_code(code, kind="策略")
