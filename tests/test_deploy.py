import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SRC_HASH = 'cat "$R/ipatool/UPSTREAM" "$R"/ipatool/patches/*.patch | sha256sum | cut -d\' \' -f1'
BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")


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
    assert text.index("trap restore_previous ERR") < swap < text.index("trap - ERR")


@pytest.mark.skipif(not BASH, reason="deployment scripts require Bash")
@pytest.mark.parametrize("empty", [None, "root", "part"])
def test_restore_refuses_empty_delete_path_before_running_commands(tmp_path, empty):
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    restore = re.search(r"(?ms)^restore_previous\(\) \{\n.*?^\}", text).group()
    previous = tmp_path / "previous"
    (previous / "appshelf").mkdir(parents=True)
    root, part = (tmp_path / "current").as_posix(), "appshelf"
    log = tmp_path / "commands.log"
    env = {**os.environ, "R": "" if empty == "root" else root, "OLD": previous.as_posix(),
           "RESTORE_PART": "" if empty == "part" else part, "RESTORE_LOG": log.as_posix()}
    # Mock destructive commands: even a broken guard cannot remove anything outside the fixture.
    wrapper = restore + '''
rm() { printf 'rm:%s\\n' "$@" >> "$RESTORE_LOG"; }
mv() { printf 'mv:%s\\n' "$@" >> "$RESTORE_LOG"; }
PARTS=("$RESTORE_PART")
restore_previous
'''
    result = subprocess.run([BASH, "-euo", "pipefail", "-c", wrapper], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=5)
    if empty:
        assert result.returncode != 0
        assert not log.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert log.read_text(encoding="utf-8").splitlines() == [
            "rm:-rf", "rm:--", f"rm:{root}/{part}",
            f"mv:{previous.as_posix()}/{part}", f"mv:{root}/{part}",
        ]


def test_install_refuses_while_nightly_check_runs():
    # ночная проверка (до 4 ч) со старым кодом в памяти догружала модули и пакеты уже после pip и подмены
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    check = text.index("systemctl is-active --quiet appshelf-nightly.service")
    assert check < text.index('pip" install') < text.index('mv "$NEW/$p" "$R/$p"')
    assert "exit 1" in text[check:text.index("fi", check)]


def install_rollback_check():
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    return text, text[start:text.index("\nPY\n", start)]


@pytest.mark.parametrize("version, readable_by, ok", [
    (11, None, True),      # схема не новее прежнего кода
    (12, "11", True),      # аддитивный переход объявил её годной прежнему коду
    (12, "12", False),     # переход несовместим — прежний код не стартует (MigrationError)
    (12, None, False),     # ключа нет — годна только своей версии
])
def test_rollback_only_when_database_is_readable_by_previous_code(tmp_path, monkeypatch, version, readable_by, ok):
    import sqlite3
    import sys
    _, check = install_rollback_check()
    db = tmp_path / "appshelf.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if readable_by:
        c.execute("INSERT INTO state VALUES ('schema_readable_by', ?)", (readable_by,))
    c.execute(f"PRAGMA user_version = {version}")
    c.commit()
    c.close()
    monkeypatch.setattr(sys, "argv", ["-", db.as_posix(), "11"])
    with pytest.raises(SystemExit) as e:
        exec(check, {})
    assert (e.value.code == 0) is ok


def test_rollback_reads_schema_version_of_previous_code():
    # install.sh берёт SCHEMA_VERSION прежнего кода sed'ом из его store.py — строка должна остаться такой
    from appshelf import store
    text, _ = install_rollback_check()
    assert "sed -n 's/^SCHEMA_VERSION = \\([0-9][0-9]*\\)$/\\1/p' \"$OLD/appshelf/store.py\"" in text
    lines = Path(store.__file__).read_text(encoding="utf-8").splitlines()
    assert [s for s in lines if re.fullmatch(r"SCHEMA_VERSION = [0-9]+", s)] == [f"SCHEMA_VERSION = {store.SCHEMA_VERSION}"]


def test_failed_healthcheck_rolls_back_to_previous_code():
    # неудачная выкладка откатывалась только вручную, по подсказке в выводе
    text, _ = install_rollback_check()
    failed = text.index("не ответил 200 на /healthz")
    check = text.index("<<'PY'", failed)
    back = text.index("  restore_previous\n", check)
    assert check < back < text.index("  install_units\n", back) < text.index("restart_and_check", back)


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
    file_installs = [line for line in build.splitlines() if "sudo install " in line and " -d " not in line]
    assert file_installs and all("-o root -g root" in line for line in file_installs)
    assert "install -o root -g root -m 0755" in build


def test_ipatool_source_hash_matches_between_build_and_deploy():
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    docker = (DEPLOY / "build-ipatool-docker.sh").read_text(encoding="utf-8")
    build = (DEPLOY / "build-ipatool.sh").read_text(encoding="utf-8")
    assert SRC_HASH in install and SRC_HASH.replace("$R", "$D") in docker
    assert '"$OUT.src-sha256"' in docker
    assert '"$R/bin/ipatool.src-sha256"' in install and '"$R/bin/ipatool.src-sha256"' in build
    # порядок файлов в глобе — побайтно, как sorted() в integrity.builds, при любой локали сервера и хоста сборки
    assert "\nexport LC_COLLATE=C" in install and "\nexport LC_COLLATE=C" in docker


@pytest.mark.skipif(not BASH, reason="deployment scripts require Bash")
@pytest.mark.parametrize("patched", [False, True])
@pytest.mark.parametrize("script", ["install.sh", "build-ipatool-docker.sh"])
def test_ipatool_source_hash_accepts_empty_patch_directory(tmp_path, script, patched):
    source = tmp_path / "ipatool"
    patches = source / "patches"
    patches.mkdir(parents=True)
    upstream = b"URL=https://example.test/ipatool.git\nCOMMIT=123\n"
    (source / "UPSTREAM").write_bytes(upstream)
    content = upstream
    if patched:
        for name, data in (("02-b.patch", b"second\n"), ("01-a.patch", b"first\n")):
            (patches / name).write_bytes(data)
        content += b"first\nsecond\n"
    text = (DEPLOY / script).read_text(encoding="utf-8")
    options = "\n".join(line for line in text.splitlines() if line.startswith("shopt "))
    command = next(line for line in text.splitlines() if SRC_HASH.replace("$R", "$D") in line
                   or SRC_HASH in line)
    if script == "install.sh":
        command += '\nprintf "%s" "$want"'
    else:
        command += '\ncat "$OUT.src-sha256"'
    env = {**os.environ, "R": tmp_path.as_posix(), "D": tmp_path.as_posix(),
           "OUT": (tmp_path / "ipatool-bin").as_posix()}
    result = subprocess.run([BASH, "-euo", "pipefail", "-c", options + "\n" + command],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashlib.sha256(content).hexdigest()


def run_mock_build(tmp_path, workdir, mktemp_status=0, other_status=0):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    script = deploy / "build-ipatool.sh"
    script.write_bytes((DEPLOY / script.name).read_bytes())
    (deploy / "build-ipatool-docker.sh").write_text("# mocked build\n", encoding="utf-8")
    log = tmp_path / "ssh.log"
    env = {**os.environ, "APPSHELF_SSH": "deploy@example.test", "APPSHELF_BUILD_SSH": "",
           "MOCK_WORKDIR": workdir, "MOCK_MKTEMP_STATUS": str(mktemp_status),
           "MOCK_OTHER_STATUS": str(other_status), "MOCK_SSH_LOG": log.as_posix()}
    wrapper = '''
ssh() {
  local command="${!#}"
  printf '%s\\0' "$command" >> "$MOCK_SSH_LOG"
  if [ "$command" = 'mktemp -d /tmp/appshelf-ipatool.XXXXXX' ]; then
    printf '%s' "$MOCK_WORKDIR"
    return "$MOCK_MKTEMP_STATUS"
  fi
  return "$MOCK_OTHER_STATUS"
}
source "$0"
'''
    result = subprocess.run([BASH, "-c", wrapper, script.as_posix()], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=5)
    return result, log.read_bytes().decode("utf-8").rstrip("\0").split("\0")


@pytest.mark.skipif(not BASH, reason="deployment scripts require Bash")
@pytest.mark.parametrize("workdir, status", [
    ("", 0), ("/", 0), ("/tmp/appshelf-ipatool.ABC", 0),
    ("/tmp/appshelf-ipatool.ABCDEF;touch /tmp/unexpected", 0),
    ("/tmp/appshelf-ipatool.ABCDEF\n/tmp/another", 0),
    ("/tmp/appshelf-ipatool.ABCDEF", 1),
])
def test_build_refuses_invalid_or_failed_remote_temp_path(tmp_path, workdir, status):
    result, commands = run_mock_build(tmp_path, workdir, mktemp_status=status)
    assert result.returncode != 0
    assert commands == ["mktemp -d /tmp/appshelf-ipatool.XXXXXX"]


@pytest.mark.skipif(not BASH, reason="deployment scripts require Bash")
@pytest.mark.parametrize("other_status", [0, 17])
def test_build_cleans_only_valid_remote_temp_path_on_success_and_failure(tmp_path, other_status):
    result, commands = run_mock_build(tmp_path, "/tmp/appshelf-ipatool.aB3dE9\n",
                                      other_status=other_status)
    assert result.returncode == other_status, result.stderr
    assert commands[-1] == "sudo -n rm -rf -- /tmp/appshelf-ipatool.aB3dE9"


def test_ipatool_built_from_deployed_sources_and_checked_on_server_before_swap():
    # сумма сверяется с выложенным: из рабочего дерева бинарник разошёлся бы с ipatool/ на сервере;
    # библиотеки проверяются на сервере (на хосте сборки они свои), и до замены рабочего бинарника
    build = (DEPLOY / "build-ipatool.sh").read_text(encoding="utf-8")
    assert '"tar -C $R -cf - ipatool"' in build
    swap = build.index('mv -f "$T/ipatool" "$R/bin/ipatool"')
    assert build.index('ldd "$T/ipatool"') < swap and build.index('"$T/ipatool" > /dev/null') < swap


def server_script():
    build = (DEPLOY / "build-ipatool.sh").read_text(encoding="utf-8")
    return build[build.index('"${BUILD[@]}" "tar -C $W -cf - bin"'):]


def test_new_binaries_run_on_server_only_as_appshelf():
    # ldd, help и --help исполняли ELF с хоста сборки от root, хотя в работе им нужен только appshelf
    script = server_script()
    runs = [s.strip() for s in script.splitlines()
            if not s.strip().startswith("#") and re.search(r'ldd |"\$T/ipatool(-auth)?" (>|--)', s)]
    assert len(runs) == 5 and all("sudo -u appshelf " in s for s in runs), runs
    assert not re.search(r'sudo "\$T|sudo ldd', script)
    # проверяемые копии ставит root со своим режимом; распакованный архив appshelf не видит
    assert 'install -d -o root -g root -m 0711 "$T"' in script
    assert script.index('install -o root -g root -m 0755 "$T/bin/ipatool" "$T/ipatool"') < script.index('ldd "$T/ipatool"')


def test_auth_cache_is_beside_binary_and_root_owned():
    # кэш ложился в <bin>/../data/cache: без ссылки data → данные install.sh отдавал его root, вход ломался;
    # а у appshelf на запись helper сверял SHA-256 Unicorn и загружал .so раздельно — файл можно было подменить
    main = (DEPLOY.parent / "ipatool" / "auth" / "main.go").read_text(encoding="utf-8")
    assert 'filepath.Join(filepath.Dir(exe),"ipatool-auth-cache")' in main and '"data"' not in main
    script = server_script()
    assert 'C="$R/bin/ipatool-auth-cache"' in script and "data/cache" not in script
    prepare = script.index('sudo -u appshelf env XDG_CACHE_HOME="$S" "$T/ipatool-auth" --prepare')
    owned = script.index('sudo chown -R root:root "$S"', prepare)
    links = script.index("-links +1", owned)
    readable = script.index('sudo chmod -R u=rwX,go=rX "$S"', links)
    assert readable < script.index('sudo mv "$S" "$C"') < script.index('mv -f "$T/ipatool-auth" "$R/bin/ipatool-auth"')


def test_build_leaves_no_preparing_helper_copy():
    script = server_script()
    assert ".ipatool-auth-preparing" not in script.replace('rm -rf "$T" "$S" "$R/bin/.ipatool-auth-preparing"', "")
    assert 'trap "sudo rm -rf $T $S" EXIT' in script


def test_web_unit_sets_tmpdir():
    # загрузка APK до 1 ГБ: multipart-разбор кладёт файл во временный каталог — не в /tmp (tmpfs, PrivateTmp)
    assert "Environment=TMPDIR=/var/lib/appshelf/tmp" in (DEPLOY / "appshelf-web.service").read_text(encoding="utf-8")


def test_vhost_serves_apk_as_attachment_and_limits_upload():
    text = (DEPLOY / "apache-appshelf.conf").read_text(encoding="utf-8")
    assert "AddType application/vnd.android.package-archive .apk" in text
    assert 'Header set Content-Disposition "attachment"' in text
    assert "LimitRequestBody 1100000000" in text


def test_vhost_upload_limit_covers_own_and_public_shelf():
    text = (DEPLOY / "apache-appshelf.conf").read_text(encoding="utf-8")
    pattern = re.search(r'<LocationMatch "([^"]+)">\s*LimitRequestBody', text).group(1)
    for path in ("/android/add/upload", "/android/apps/ru.x/upload", "/android/public/add/upload",
                 "/android/public/apps/ru.x/upload"):
        assert re.match(pattern, path), path
    assert not re.match(pattern, "/android/add/link")


def test_vhost_sets_headers_the_code_relies_on():
    # заголовки жили только в глобальной настройке боевого сервера: по образцу их не восстановить
    text = (DEPLOY / "apache-appshelf.conf").read_text(encoding="utf-8")
    vhost = text[text.index("<VirtualHost"):text.index("<Location />")]
    assert 'Header setifempty Referrer-Policy "same-origin"' in vhost and "no-referrer\"" not in vhost
    assert 'Header always set X-Content-Type-Options "nosniff"' in vhost
    assert 'Header always set Strict-Transport-Security "max-age=' in vhost


def test_units_allow_google_dir():
    # мастер-токены Google Play — /etc/appshelf/google/<id>/ (spec 2026-10-28 §6.4); ProtectSystem=full закрыл бы /etc
    for unit in ("appshelf-web.service", "appshelf-nightly.service"):
        line = next(s for s in (DEPLOY / unit).read_text(encoding="utf-8").splitlines() if s.startswith("ReadWritePaths="))
        assert "/etc/appshelf/google" in line.split("=", 1)[1].split(), unit
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    made = install.index("install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /etc/appshelf/google")
    assert made < install.index("systemctl restart appshelf-web")
