from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "deploy.sh"


def test_owner_checked_before_code_is_installed():
    # проверка после установки оставляла новый код без APPSHELF_OWNER: ближайший перезапуск — MigrationError
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index('grep -qE "^APPSHELF_OWNER=.+"') < text.index("sudo tar -xzf")
