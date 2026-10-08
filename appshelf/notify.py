"""Письма владельцу: одно на эпизод (метка в state или в accounts), через локальный sendmail (exim4)."""
from __future__ import annotations

import socket
import subprocess
import traceback
from email.message import EmailMessage

from . import store

HOST = socket.gethostname()
MAIL_FROM = f'"[{HOST}] appshelf" <appshelf@{HOST}>'
LOW_SPACE_KEY = "low_space_mail_sent"


def send_mail(subject: str, body: str, to: str) -> None:
    """EmailMessage кодирует кириллицу в теме, чего не делает `mail -s`."""
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, MAIL_FROM, to
    msg.set_content(body)
    subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"], input=msg.as_bytes(), check=True, timeout=60)


def once_for(claim, release, subject: str, body: str, send, to: str) -> bool:
    """Письмо, если claim() поставил метку эпизода. Не ушло — release(): повторим в следующий раз."""
    if not to or not claim():
        return False
    try:
        send(subject, body, to)
    except Exception:  # сбой почты не должен останавливать обработчик и ночную проверку
        traceback.print_exc()
        release()
        return False
    return True


def once(c, key: str, subject: str, body: str, send, to: str, now: str) -> bool:
    """Письмо на эпизод с меткой в state — общей для сервера («мало места»)."""
    return once_for(lambda: store.claim_state(c, key, now), lambda: store.set_state(c, key, ""),
                    subject, body, send, to)


def send_quietly(send, subject: str, body: str, to: str) -> None:
    """Письмо без эпизода (новый участник): сбой почты не ломает вход."""
    if not to:
        return
    try:
        send(subject, body, to)
    except Exception:
        traceback.print_exc()


def reset(c, key: str) -> None:
    store.set_state(c, key, "")
