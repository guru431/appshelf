from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SRC_HASH = 'cat "$R/ipatool/UPSTREAM" "$R"/ipatool/patches/*.patch | sha256sum | cut -d\' \' -f1'


def test_only_committed_head_is_deployed():
    # рабочее дерево уезжало с неотслеживаемыми файлами, а tar поверх не удалял убранные из репозитория
    text = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    assert text.index("status --porcelain") < text.index('git -C "$D" archive --format=tar.gz HEAD')


def test_new_code_goes_live_only_after_checks_and_pip():
    # проверка после установки оставляла новый код без APPSHELF_OWNER: ближайший перезапуск — MigrationError;
    # упавший pip — новый код со старыми зависимостями
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    swap = text.index('mv "$NEW/$p" "$R/$p"')
    assert text.index('grep -qE "^APPSHELF_OWNER=.+"') < swap and text.index("mountpoint -q") < swap
    assert text.index("pip\" install") < swap and "--no-deps --force-reinstall" in text


def test_code_and_binary_belong_to_root():
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    build = (DEPLOY / "build-ipatool.sh").read_text(encoding="utf-8")
    assert 'chown -R root:"$GROUP" "$NEW"' in install and "-user appshelf -exec chown -h root:root" in install
    assert "install -o root -g root -m 0755" in build and "-o appshelf" not in build


def test_ipatool_source_hash_matches_between_build_and_deploy():
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    docker = (DEPLOY / "build-ipatool-docker.sh").read_text(encoding="utf-8")
    build = (DEPLOY / "build-ipatool.sh").read_text(encoding="utf-8")
    assert SRC_HASH in install and SRC_HASH.replace("$R", "$D") in docker
    assert '"$OUT.src-sha256"' in docker
    assert '"$R/bin/ipatool.src-sha256"' in install and '"$R/bin/ipatool.src-sha256"' in build


def test_ipatool_built_from_deployed_sources_and_checked_on_server_before_swap():
    # сумма сверяется с выложенным: из рабочего дерева бинарник разошёлся бы с ipatool/ на сервере;
    # библиотеки проверяются на сервере (на хосте сборки они свои), и до замены рабочего бинарника
    build = (DEPLOY / "build-ipatool.sh").read_text(encoding="utf-8")
    assert '"tar -C $R -cf - ipatool"' in build
    swap = build.index('mv -f "$T/ipatool" "$R/bin/ipatool"')
    assert build.index('sudo ldd "$T/bin/ipatool"') < swap and build.index('"$T/bin/ipatool" help') < swap
