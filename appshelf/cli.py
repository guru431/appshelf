"""CLI appshelf: ночная проверка (appshelf-nightly.service), запасная ссылка входа — если вход Apple сломан, а живой
cookie нет и у владельца, — новый токен каталога полки, если её ссылки установки утекли, проверка целостности полок и
снимок базы для восстановления. Только от appshelf."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from . import integrity, ipatool, jobs, nightly, people, store
from .android import db as android_db, fetch as android_fetch, net, play
from .android.sources import Sources
from .config import from_env


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appshelf")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly", help="ночная проверка: история, новые версии, отметка для Zabbix")
    link = sub.add_parser("login-link", help="запасная ссылка входа: одноразовая, 24 ч")
    link.add_argument("--email", required=True, help="любой Apple ID человека")
    rotate = sub.add_parser("rotate-token", help="новый токен каталога полки: прежние ссылки установки перестанут "
                                                 "работать, установленные приложения — останутся")
    whose = rotate.add_mutually_exclusive_group(required=True)
    whose.add_argument("--email", help="Apple ID полки")
    whose.add_argument("--public", action="store_true", help="публичная Android-полка (ссылки гостей)")
    rotate.add_argument("--android", action="store_true", help="с --email: Android-полка участника этого Apple ID")
    chk = sub.add_parser("check", help="проверка целостности полок, только чтение: код 0 — проблем нет, 1 — есть, "
                                       "2 — проверка не выполнена")
    chk.add_argument("--deep", action="store_true", help="ещё SHA-256 и содержимое каждого IPA (читает весь архив)")
    chk.add_argument("--json", action="store_true", help="отчёт в JSON")
    chk.add_argument("--db", type=Path, help="снимок базы (appshelf backup) вместо живой")
    chk.add_argument("--archive", type=Path, help="копия архива вместо APPSHELF_PUB")
    bak = sub.add_parser("backup", help="согласованный снимок базы: --out — набор восстановления (архив — копией, "
                                        "затем check), --daily — ночной снимок в APPSHELF_BACKUP_DIR")
    how = bak.add_mutually_exclusive_group(required=True)
    how.add_argument("--out", type=Path, help="новый или пустой каталог")
    how.add_argument("--daily", action="store_true", help="appshelf-<дата>.db в APPSHELF_BACKUP_DIR "
                                                          "(appshelf-backup.timer)")
    bak.add_argument("--keep", type=int, default=14, help="сколько ночных снимков хранить (по умолчанию 14)")
    args = p.parse_args(argv)
    if args.cmd == "backup" and args.keep < 1:
        p.error("--keep — от 1")
    if args.cmd == "rotate-token" and args.android and not args.email:
        p.error("--android — только с --email")
    cfg = from_env()
    jobs.check_user(cfg)
    if args.cmd == "check":
        return check(cfg, args)
    if args.cmd == "backup":
        return daily(cfg, args.keep) if args.daily else backup(cfg, args.out)
    with closing(store.connect(cfg.db, cfg.owner_email)) as c:
        if args.cmd == "login-link" or args.cmd == "rotate-token" and args.email:
            acct = people.account_by_email(c, args.email)
            if acct is None:
                print("такого Apple ID на сервере нет", file=sys.stderr)
                return 2
        if args.cmd == "login-link":
            print(f"{cfg.public_base}/l/{people.create_link(c, people.LOGIN, '', acct.user_id, jobs.now_iso())}")
            return 0
        if args.cmd == "rotate-token":
            return rotate_token(cfg, c, args, acct if args.email else None)
        print(nightly.run(jobs.Env(cfg, lambda a: ipatool.for_account(cfg, a),
                                   sources=Sources(net.Http(cfg.sources_ca), play=play.PlayPool(cfg))), c))
    return 0


def rotate_token(cfg, c, args, acct) -> int:
    """Полка Apple ID, Android-полка его участника (--android) или публичная (--public)."""
    shelf = None
    if args.public or args.android:
        shelf = android_db.public_shelf(c) if args.public else android_db.shelf_for_user(c, acct.user_id)
        if shelf is None:
            print("такой Android-полки нет: на неё ещё ничего не добавляли", file=sys.stderr)
            return 2
    try:
        if shelf is None:
            jobs.move_shelf(cfg, c, acct)
        else:
            android_fetch.rotate_shelf(cfg, c, shelf.id)
    except jobs.ArchiveUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    except jobs.AccountBusy:
        print("идёт скачивание или задание этой полки — повторите позже", file=sys.stderr)
        return 1
    print("готово: каталог полки переименован, ссылки " + ("на файлы" if shelf else "установки") + " — новые")
    return 0


def check(cfg, args) -> int:
    """Только чтение: база открыта mode=ro (без перехода схемы), архив не меняется. --db и --archive — проверка
    набора восстановления: снимок и копия архива, без обращений к Apple и писем."""
    if args.archive:
        cfg = replace(cfg, pub_dir=args.archive)
    try:
        with closing(integrity.open_ro(args.db or cfg.db)) as c:
            rep = integrity.check(cfg, c, deep=args.deep)
    except (integrity.DbError, jobs.ArchiveUnavailable) as e:
        print(e, file=sys.stderr)
        return 2
    print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=1) if args.json else rep.text())
    return 0 if rep.ok else 1


def backup(cfg, out: Path) -> int:
    try:
        dst = integrity.backup(cfg.db, out)
    except (integrity.DbError, integrity.BackupError) as e:
        print(e, file=sys.stderr)
        return 2
    print(f"снимок базы: {dst}\n"
          f"дальше:\n"
          f"  1. скопируйте архив {cfg.archive} (например, rsync -a {cfg.archive}/ {out / 'pub'}/)\n"
          f"  2. проверьте копию: appshelf check --db {dst} --archive {out / 'pub'} [--deep]\n"
          f"  пропавшие файлы или каталоги — между снимком и копией прошла ротация версий или rotate-token: "
          f"повторите набор\n"
          f"  appshelf.env и cookie-key — отдельно, в защищённое место")
    return 0


def daily(cfg, keep: int) -> int:
    """Ночной снимок (appshelf-backup.timer). Сбой — код 2 и строка в журнале службы."""
    if cfg.backup_dir is None:
        print("не задан APPSHELF_BACKUP_DIR в appshelf.env — каталог ночных снимков", file=sys.stderr)
        return 2
    try:
        dst = integrity.daily_backup(cfg.db, cfg.backup_dir, cfg.tmp_dir / "backup", jobs.now_iso()[:10], keep)
    except (integrity.DbError, integrity.BackupError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    print(f"снимок базы: {dst}")
    return 0
