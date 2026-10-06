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
