# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-12 · Отказ Apple «account is disabled» показывается сырым текстом [P3]
**Context:** первый вход приглашённого участника: форма показала «Ошибка входа: account is disabled», владелец счёл это сбоем сервера. Проверка тем же паролем с тремя MAC (Apple OUI, Hyper-V OUI, настоящий MAC сервера) — везде тот же ответ Apple, т. е. дело в самой учётке.
**What:** ipatool отдаёт этот отказ как `{"error":"error","message":"account is disabled"}`, `LOGIN_ERRORS` его не знает — человек видит английскую фразу без объяснения и пробует снова (каждая попытка — лишний неудачный вход в Apple ID).
**Proposal:** в патче `04-exit-codes` отдельный код `account_disabled` (`fail_from`), в `LOGIN_ERRORS` — текст «Apple отключил покупки (App Store) для этого Apple ID — проверьте App Store на iPhone; сервер тут ни при чём, повторять вход бесполезно»; тест в `tests/test_signin.py`.
**Status:** open
