from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SRC_HASH = 'cat "$R/ipatool/UPSTREAM" "$R"/ipatool/patches/*.patch | sha256sum | cut -d\' \' -f1'


def test_only_committed_head_is_deployed():
    # рабочее дерево уезжало с неотслеживаемыми файлами, а tar поверх не удалял убранные из репозитория
    text = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    assert text.index("status --porcelain") < text.index('git -C "$D" archive --format=tar.gz HEAD')


def test_code_group_reaches_server_as_one_shell_word():
    # значение из .env вставлялось в удалённую команду между кавычками как есть: $(…) или " в нём исполнил бы сервер;
    # printf %q давал $'…', а его разбирает только bash — не любая оболочка входа на сервере
    text = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    check = text.index('[[ ! "$GROUP" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]]')
    assert check < text.index('git -C "$D" archive --format=tar.gz HEAD') and "%q" not in text
    assert text.rstrip().endswith("""install.sh" '"$GROUP\"""")


def test_new_code_goes_live_only_after_checks_and_pip():
    # проверка после установки оставляла новый код без APPSHELF_OWNER: ближайший перезапуск — MigrationError;
    # упавший pip — новый код со старыми зависимостями; www-data, читающий секреты, ловился уже после перезапуска
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    swap = text.index('mv "$NEW/$p" "$R/$p"')
    assert text.index('grep -qE "^APPSHELF_OWNER=.+"') < swap and text.index("mountpoint -q") < swap
    assert text.index('sudo -u www-data test -r "$ENV_FILE"') < swap
    assert text.index("pip\" install") < swap and "--no-deps --force-reinstall" in text


def test_failed_swap_puts_previous_code_back():
    # mv, упавший посреди подмены, оставлял на диске смесь нового и прежнего кода
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    swap = text.index('mv "$NEW/$p" "$R/$p"')
    assert text.index("' ERR") < swap < text.index("trap - ERR")


def test_build_refuses_host_without_machine_id():
    # на месте отсутствующего файла Docker создал бы каталог — и в /etc хоста сборки
    docker = (DEPLOY / "build-ipatool-docker.sh").read_text(encoding="utf-8")
    assert docker.index("[ ! -s /etc/machine-id ]") < docker.index("-v /etc/machine-id:/etc/machine-id:ro")


def test_backup_timer_installed_and_enabled_only_with_backup_dir():
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    service = (DEPLOY / "appshelf-backup.service").read_text(encoding="utf-8")
    timer = (DEPLOY / "appshelf-backup.timer").read_text(encoding="utf-8")
    assert "appshelf-backup.service appshelf-backup.timer" in install
    assert install.index('grep -qE "^APPSHELF_BACKUP_DIR=.+"') < install.index("enable --quiet --now appshelf-backup")
    assert "User=appshelf" in service and "-m appshelf backup --daily" in service
    assert "OnCalendar=*-*-* 05:30:00" in timer and "Persistent=true" in timer


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


def test_web_unit_sets_tmpdir():
    # загрузка APK до 1 ГБ: multipart-разбор кладёт файл во временный каталог — не в /tmp (tmpfs, PrivateTmp)
    assert "Environment=TMPDIR=/var/lib/appshelf/tmp" in (DEPLOY / "appshelf-web.service").read_text(encoding="utf-8")


def test_vhost_serves_apk_as_attachment_and_limits_upload():
    text = (DEPLOY / "apache-appshelf.conf").read_text(encoding="utf-8")
    assert "AddType application/vnd.android.package-archive .apk" in text
    assert 'Header set Content-Disposition "attachment"' in text
    assert "LimitRequestBody 1100000000" in text
