"""CLI appshelf: ночная проверка (appshelf-nightly.service) и запасная ссылка входа — если вход Apple сломан,
а живой cookie нет и у владельца."""
from __future__ import annotations

import argparse
import sys
from contextlib import closing

from . import ipatool, jobs, nightly, people, store
from .config import from_env


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appshelf")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly", help="ночная проверка: история, новые версии, отметка для Zabbix")
    link = sub.add_parser("login-link", help="запасная ссылка входа: одноразовая, 24 ч")
    link.add_argument("--email", required=True, help="любой Apple ID человека")
    args = p.parse_args(argv)
    cfg = from_env()
    with closing(store.connect(cfg.db, cfg.owner_email)) as c:
        if args.cmd == "login-link":
            acct = people.account_by_email(c, args.email)
            if acct is None:
                print("такого Apple ID на сервере нет", file=sys.stderr)
                return 2
            print(f"{cfg.public_base}/l/{people.create_link(c, people.LOGIN, '', acct.user_id, jobs.now_iso())}")
            return 0
        print(nightly.run(jobs.Env(cfg, lambda a: ipatool.for_account(cfg, a)), c))
    return 0
