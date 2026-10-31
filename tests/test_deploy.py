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
    assert build.index('sudo ldd "$T/bin/ipatool"') < swap and build.index('"$T/bin/ipatool" help') < swap


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


def test_units_allow_google_dir():
    # мастер-токены Google Play — /etc/appshelf/google/<id>/ (spec 2026-10-28 §6.4); ProtectSystem=full закрыл бы /etc
    for unit in ("appshelf-web.service", "appshelf-nightly.service"):
        line = next(s for s in (DEPLOY / unit).read_text(encoding="utf-8").splitlines() if s.startswith("ReadWritePaths="))
        assert "/etc/appshelf/google" in line.split("=", 1)[1].split(), unit
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    made = install.index("install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /etc/appshelf/google")
    assert made < install.index("systemctl restart appshelf-web")
