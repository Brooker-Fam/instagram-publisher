import getpass
import sys
import warnings

import httpx
import pytest

from instagram_publisher import cli


@pytest.fixture
def unified_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_API_KEY", "synthetic-admin-key-for-cli-tests-000000")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://publisher.example.test")
    monkeypatch.setattr(sys, "argv", ["publisher", "import-unified", "--username", "demo_profile"])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"username": "demo_profile", "auth_provider": "UNIFIED"})

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond))
    )
    return requests


def test_unified_import_uses_hidden_input_and_does_not_print_token(monkeypatch, capsys, unified_cli):
    token = "synthetic-unified-token-for-offline-cli-test"
    monkeypatch.setattr(getpass, "getpass", lambda prompt: token)

    cli.main()

    assert len(unified_cli) == 1
    request = unified_cli[0]
    assert request.method == "POST"
    assert request.url.path == "/api/accounts/import-unified-token"
    assert token not in str(request.url)
    assert request.content == (
        b'{"access_token":"synthetic-unified-token-for-offline-cli-test","username":"demo_profile"}'
    )
    output = capsys.readouterr()
    assert "demo_profile" in output.out
    assert token not in output.out + output.err


def test_unified_import_refuses_visible_password_fallback(monkeypatch, capsys, unified_cli):
    def unsafe_prompt(prompt):
        warnings.warn("Password input may be echoed", getpass.GetPassWarning)
        pytest.fail("Token entry must stop before the visible-input fallback")

    monkeypatch.setattr(getpass, "getpass", unsafe_prompt)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert not unified_cli
    assert "hidden input" in capsys.readouterr().err


def test_unified_import_rejects_empty_token(monkeypatch, capsys, unified_cli):
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "   ")

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert not unified_cli
    assert "token is required" in capsys.readouterr().err


def test_unified_import_redacts_token_before_json_escaping(monkeypatch, capsys, unified_cli):
    token = 'synthetic"quote\\token'
    monkeypatch.setattr(getpass, "getpass", lambda prompt: token)
    monkeypatch.setattr(httpx.Response, "json", lambda self: {"detail": [{"input": token}]})

    cli.main()

    assert '"input": "[redacted]"' in capsys.readouterr().out
