"""CLI appshelf: ночная проверка (appshelf-nightly.service) и пароль страниц."""
from __future__ import annotations

import argparse
import sys
from contextlib import closing

from . import ipatool, jobs, nightly, store, webauth
from .config import from_env


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appshelf")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly", help="ночная проверка: история, новые версии, отметка для Zabbix")
    pw = sub.add_parser("set-web-password", help="пароль страниц (тот же, что в htpasswd) — со stdin")
    pw.add_argument("--user", default="admin")
    args = p.parse_args(argv)
    cfg = from_env()
    if args.cmd == "set-web-password":
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            print("пустой пароль", file=sys.stderr)
            return 2
        cfg.web_auth.parent.mkdir(parents=True, exist_ok=True)
        webauth.set_password(cfg.web_auth, args.user, password)
        return 0
    with closing(store.connect(cfg.db)) as c:
        print(nightly.run(jobs.Env(cfg, ipatool.from_config(cfg)), c))
    return 0
