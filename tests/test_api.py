from pathlib import Path

from tokenledger.api import _static_content_type


def test_javascript_uses_executable_mime_type_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("tokenledger.api.mimetypes.guess_type", lambda _: ("text/plain", None))
    assert _static_content_type(Path("app.js")) == "application/javascript"


def test_common_static_content_types_are_explicit() -> None:
    assert _static_content_type(Path("app.css")) == "text/css"
    assert _static_content_type(Path("index.html")) == "text/html"
    assert _static_content_type(Path("mark.svg")) == "image/svg+xml"
