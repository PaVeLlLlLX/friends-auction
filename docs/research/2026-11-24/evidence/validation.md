# Протокол проверки

Дата: 24.11.2026. Исходный commit: `45ccee95610328be1bdb23e5ae81325b6540f4d6`.
Сводный [отчёт](../report.md) задаёт область и ограничения. Настоящий документ фиксирует выполненные команды и происхождение сохранённых результатов.

## Штатные проверки

Из корня репозитория, существующее окружение Python 3.12.14 / macOS arm64 / SQLite 3.53.4:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m mypy
```

Результаты: `54 passed in 1.22s`, `All checks passed!`, `26 files already formatted`, `Success: no issues found in 19 source files`. Все команды завершились с кодом 0.

Node/npm не доступны в PATH. Для двух штатных frontend-проверок использован уже установленный Node из приложения ChatGPT:

```sh
/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node node_modules/typescript/bin/tsc --project jsconfig.json
/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node node_modules/prettier/bin/prettier.cjs --check static
```

Обе команды завершились с кодом 0. Prettier: `All matched files use Prettier code style!`. Это проверка локального исходного снимка, не отдельная чистая установка зависимостей.

## Изолированные проверки

| Протокол                                                                                                             | Что действительно сделано                                                                                                                                                                                             |
| -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [correctness-receipts.json](correctness-receipts.json)                                                               | Три контрпримера и три положительных контроля; реальные request-threads/SQLite, fake identities; управляемые часы/барьеры там, где указано. Основной агент отдельно повторил три контрпримера с теми же результатами. |
| [security-probes.json](security-probes.json)                                                                         | Роли, изменённая подпись, повторное использование initData, API/photo throttle coverage и заголовки на Flask test client.                                                                                             |
| [gunicorn-parser-probe.json](gunicorn-parser-probe.json)                                                             | Конечный unit input в ChunkedReader 26.2.0; без сокетов/публичного сервиса. Не доказательство внешнего DoS.                                                                                                           |
| [pip-audit-requirements.json](pip-audit-requirements.json)                                                           | Свежая резолюция четырёх прямых runtime requirements и транзитивных пакетов; 10 пакетов, `vulns=[]`.                                                                                                                  |
| [pip-audit-installed-runtime.json](pip-audit-installed-runtime.json), [installed-runtime.txt](installed-runtime.txt) | Аудит тех же 10 runtime-пакетов по реально установленным локальным версиям; `vulns=[]`. Рабочее развёртывание не инвентаризировалось.                                                                                 |
| [ux-browser-results.json](ux-browser-results.json)                                                                   | Headless Chrome: Enter, нулевая ставка с confirm, архивная история и commit create с потерей ответа.                                                                                                                  |
| [ux-more-results.json](ux-more-results.json)                                                                         | Headless Chrome: свежий статус архивной истории, сохранение draft/фокуса input и потеря фокуса кнопки при polling; computed styles.                                                                                   |
| [ux-contrast.json](ux-contrast.json)                                                                                 | Расчёт относительной яркости/контраста по проверенным цветам.                                                                                                                                                         |
| [restore-receipt.json](restore-receipt.json)                                                                         | Синтетический idle backup БД через SQLite API плюс копия JPEG, восстановление в другой каталог, integrity/FK/history/photo/hash.                                                                                      |

JSON сохранены из файлов или stdout успешных проверок. Транскрипция stdout явно обозначена в соответствующем receipt и не считается дополнительным запуском. Управляемые временные тестовые БД конкурентных/security/restore проверок удалены. Браузерная fixture-БД содержит только вымышленные данные и осталась в `/tmp/friends-auction-audit-20261124/ux-fixtures`; она не входит в репозиторий. Локальный сервер на 19427 остановлен, временные Chrome закрыты.

Временные драйверы проверок находятся вне репозитория в `/tmp/friends-auction-audit-20261124/`: `reproduce_correctness.py`, `verify_correctness_controls.py`, `restore_probe.py`, `ux_server.py`, `ux_browser.cjs`, `ux_more.cjs`. Они зависят от существующего локального tooling и не поставляются как переносимая автоматическая suite. В отчётах приведены условия воспроизведения и приёмочные сценарии для переноса в regression-тесты. Долговечный состав отчёта — Markdown и сохранённые JSON, а не обещание сохранности `/tmp`.

## Что не выполнено

- Docker runtime: daemon отсутствует; `docker version` не подключился к `/var/run/docker.sock`.
- Compose validation: `docker compose version` сообщает `unknown command`; конфигурация прочитана статически. Старый CI smoke не объявляется текущим прогоном.
- Реальные Telegram iOS/Android, Safari/WebKit, экранная клавиатура, screen readers и настоящая доставка.
- Внешнее нагрузочное/DoS-испытание и проверка сырого HTTP framing через Cloudflare.
- Перезапуск живой launchd-службы, отключение питания/сети/диска, восстановление настоящего backup.
- Проверка всей истории Git на секреты, актуальных защит GitHub и всех системных/native зависимостей.

## Источники, формат и целостность

Ссылки на код привязаны к указанному commit. Внешние источники и их ограничения приведены рядом с выводами; реестр ссылок не считается числом полностью проверенных независимых публикаций.

`SHA256SUMS` фиксирует байты сохранённых Markdown/JSON/инвентаризации после форматирования. Он предназначен для проверки целостности этого комплекта и не доказывает сам по себе истинность результатов, идентичность запущенного сервера или безопасность приложения.
