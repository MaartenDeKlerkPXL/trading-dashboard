import pytest


@pytest.fixture(autouse=True)
def never_real_secrets_or_email(monkeypatch):
    """Tests never read the real .env and can never send a real e-mail."""
    monkeypatch.setattr("backend.main.load_env", lambda: {})

    def refuse(*args, **kwargs):
        raise AssertionError("Tests must never send real e-mail")

    monkeypatch.setattr("smtplib.SMTP", refuse)
