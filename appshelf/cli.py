"""CLI appshelf: ночная проверка (appshelf-nightly.service), запасная ссылка входа — если вход Apple сломан, а живой
cookie нет и у владельца, — и новый токен каталога полки, если её ссылки установки утекли. Только от appshelf."""
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
    rotate = sub.add_parser("rotate-token", help="новый токен каталога полки: прежние ссылки установки перестанут "
                                                 "работать, установленные приложения — останутся")
    rotate.add_argument("--email", required=True, help="Apple ID полки")
    args = p.parse_args(argv)
    cfg = from_env()
    jobs.check_user(cfg)
    with closing(store.connect(cfg.db, cfg.owner_email)) as c:
        if args.cmd in ("login-link", "rotate-token"):
            acct = people.account_by_email(c, args.email)
            if acct is None:
                print("такого Apple ID на сервере нет", file=sys.stderr)
                return 2
        if args.cmd == "login-link":
            print(f"{cfg.public_base}/l/{people.create_link(c, people.LOGIN, '', acct.user_id, jobs.now_iso())}")
            return 0
        if args.cmd == "rotate-token":
            try:
                jobs.move_shelf(cfg, c, acct)
            except jobs.ArchiveUnavailable as e:
                print(e, file=sys.stderr)
                return 1
            except jobs.AccountBusy:
                print("идёт скачивание или задание этого Apple ID — повторите позже", file=sys.stderr)
                return 1
            print("готово: каталог полки переименован, ссылки установки — новые")
            return 0
        print(nightly.run(jobs.Env(cfg, lambda a: ipatool.for_account(cfg, a)), c))
    return 0
