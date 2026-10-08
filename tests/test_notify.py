import socket
import subprocess
from email import message_from_bytes, policy

from appshelf import notify


def test_send_mail_from_this_host(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append((args, kw["input"])))
    notify.send_mail("appshelf: тема", "текст", "owner@example.com")
    args, raw = calls[0]
    assert args == ["/usr/sbin/sendmail", "-t", "-oi"]
    msg = message_from_bytes(raw, policy=policy.default)
    host = socket.gethostname()
    assert msg["From"] == f'"[{host}] appshelf" <appshelf@{host}>'
    assert msg["Subject"] == "appshelf: тема" and msg["To"] == "owner@example.com"


def test_once_for_releases_mark_when_mail_fails():
    marks = []

    def boom(subject, body, to):
        raise OSError("exim недоступен")

    assert notify.once_for(lambda: marks.append("claim") or True, lambda: marks.append("release"),
                           "тема", "текст", boom, "owner@example.com") is False
    assert marks == ["claim", "release"]
    assert notify.once_for(lambda: True, lambda: None, "тема", "текст", boom, "") is False   # некому писать


def test_send_quietly_survives_mail_errors():
    def boom(subject, body, to):
        raise OSError("exim недоступен")

    notify.send_quietly(boom, "тема", "текст", "owner@example.com")   # не бросает
