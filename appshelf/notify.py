"""Письма владельцу: одно на эпизод (метка в state), через локальный sendmail (exim4)."""
from __future__ import annotations

import socket
import subprocess
import traceback
from email.message import EmailMessage

from . import store

HOST = socket.gethostname()
MAIL_FROM = f'"[{HOST}] appshelf" <appshelf@{HOST}>'
EXPIRED_KEY = "expired_mail_sent"
LOW_SPACE_KEY = "low_space_mail_sent"


def send_mail(subject: str, body: str, to: str) -> None:
    """EmailMessage кодирует кириллицу в теме, чего не делает `mail -s`."""
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, MAIL_FROM, to
    msg.set_content(body)
    subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"], input=msg.as_bytes(), check=True, timeout=60)


def once(c, key: str, subject: str, body: str, send, to: str, now: str) -> bool:
    """Письмо, если в этом эпизоде его ещё не было. Не ушло — метку снимаем: повторим в следующий раз."""
    if not to or not store.claim_state(c, key, now):
        return False
    try:
        send(subject, body, to)
    except Exception:  # сбой почты не должен останавливать обработчик и ночную проверку
        traceback.print_exc()
        store.set_state(c, key, "")
        return False
    return True


def reset(c, key: str) -> None:
    store.set_state(c, key, "")
