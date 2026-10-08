"""CLI appshelf: ночная проверка (appshelf-nightly.service)."""
from __future__ import annotations

import argparse
from contextlib import closing

from . import ipatool, jobs, nightly, store
from .config import from_env


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appshelf")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly", help="ночная проверка: история, новые версии, отметка для Zabbix")
    p.parse_args(argv)
    cfg = from_env()
    with closing(store.connect(cfg.db, cfg.owner_email)) as c:
        print(nightly.run(jobs.Env(cfg, lambda acct: ipatool.for_account(cfg, acct)), c))
    return 0
