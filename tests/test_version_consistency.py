from __future__ import annotations

import re
from pathlib import Path

import tokenledger
from tokenledger import native, __version__


def test_version_consistency_across_metadata() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    # 1. tokenledger.__version__ matches native.APP_VERSION
    assert native.APP_VERSION == __version__

    # 2. pyproject.toml matches __version__
    pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject_text

    # 3. web/index.html contains exact brand-tag-pill
    html_text = (repo_root / "web" / "index.html").read_text(encoding="utf-8")
    assert f'<span class="brand-tag-pill">v{__version__}</span>' in html_text

    # 4. README.md matches __version__
    readme_text = (repo_root / "README.md").read_text(encoding="utf-8")
    assert f'当前版本：{__version__}。' in readme_text
