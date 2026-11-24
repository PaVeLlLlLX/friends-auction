# Friends Auction: безопасность и зависимости

Дата проверки: 24 ноября 2026 года. Объект: исходный код репозитория Friends Auction, commit `45ccee95610328be1bdb23e5ae81325b6540f4d6`, тег `v1.1.0` указывает на этот commit. Это исследовательская проверка снимка; работающий сервис, токены, настоящая БД, фотографии и аккаунты не использовались. Исходники не изменялись.

## Вывод

В прочитанных обработчиках не обнаружен подтверждённый обход авторизации, раскрывающий заявки или чужую историю ставок. Роли проверяются на сервере по подписанному числовому Telegram ID. В выполненных локальных проверках анонимный запрос к управлению получил 401, подписанный неодобренный аккаунт — 403, синтетический организатор — 200, изменённая подпись организатора — 401. Это подтверждение конкретных сценариев, а не гарантия отсутствия иных уязвимостей.

Главные оставшиеся риски безопасности относятся к доступности публичного сервиса и воспроизводимости окружения: лимиты приложения не охватывают фотографии/HTML, Gunicorn закреплён до последующего усиления парсера HTTP/1, а транзитивные Python-зависимости не закреплены. Для этого масштаба не нужна смена Flask/SQLite или дополнительная сервисная архитектура.

## Модель угроз и существующие меры

| Субъект                                       | Реальные возможности и границы                                                                                                                                                                                                                                                  |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Посторонний посетитель                        | Публичные карточки, фотографии, HTML/assets; неподписанный запрос не получает профиль или роль. Может создавать сетевую нагрузку без заявки.                                                                                                                                    |
| Подписанный, но неодобренный пользователь     | Может создать собственный профиль/заявку, смотреть свою историю, подписываться на лоты; делать ставки и управлять чужими данными не может. Подписка без допуска является заявленным поведением.                                                                                 |
| Одобренный участник                           | Делает ставки и управляет собственными подписками; допуск повторно проверяется под блокировкой записи. Наличие допуска не отменяет лимиты запросов и проверки цены/шага.                                                                                                        |
| Обладатель действующего initData организатора | Имеет права организатора до истечения принятого 24-часового окна. Изменить подписанные поля без токена бота не может. Это bearer-подобная модель; утечка организаторского initData или компрометация его Telegram-аккаунта находится за пределами проверки дружеского username. |
| Оператор/локальная компрометация              | Может читать диск/процессы/конфигурацию, ошибочно открыть demo, запустить другую версию или отключить worker. Контейнер уменьшает привилегии процесса, но не защищает от администратора хоста.                                                                                  |

Подтверждено кодом:

- `verify_telegram`, [app.py:92–130](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L92-L130): HMAC-SHA256, constant-time comparison, запрет повторяющихся query-полей, ограничение длины, положительный числовой ID, давность до 24 часов и будущая дата не более 30 секунд. Методика HMAC соответствует [официальной инструкции Telegram](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Telegram предлагает проверять давность; конкретный 24-часовой срок выбирает приложение.
- `admin_required`, [app.py:363–383](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L363-L383): username не является ключом роли. Административные endpoints защищены этим декоратором; пользовательская история фильтруется по ID текущего пользователя.
- `lot_view`, [app.py:421–477](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L421-L477): отдельная публичная проекция; имена и ID участников не включаются. Приватные кандидаты/все ставки возвращаются отдельным admin-обработчиком.
- SQL использует параметры. Конкатенация имени флага подписки происходит после whitelist `watching`/`participation`; имена столбцов миграции заданы константами. Прочитанные пути не дали контрпримера SQL injection.
- `headers`, [app.py:202–213](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L202-L213): CSP без inline/eval, `nosniff`, `no-referrer`, API `no-store`. В `static/app.js` произвольные строковые поля экранируются `esc`, live-поля обновляются `textContent`; шаблон Jinja содержит доверенные локальные сообщения. Это статический разбор, не полноценный браузерный XSS-тест.
- Авторизация передаётся в `X-Telegram-Init-Data`, а не автоматически прикладываемой cookie. Классический CSRF через стороннюю HTML-форму не получает эту подпись; код не включает разрешительный CORS. Подмена заголовка без валидной подписи роли не даёт. [Flask описывает CSRF-модель именно в связи с автоматически передаваемыми cookies](https://flask.palletsprojects.com/en/stable/web-security/#cross-site-request-forgery-csrf).
- `cloudflare_client_ip`, [app.py:251–266](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L251-L266): `CF-Connecting-IP` используется только при явном флаге и loopback peer; произвольные `X-Forwarded-For` не дают доверенного IP. Реальный туннель и конфигурация edge здесь не проверялись.
- Загрузка доступна только организатору. Установлены 36 MiB на запрос, 12 multipart-частей, 6 фотографий, 20 млн пикселей; исходники перекодируются в JPEG, метаданные очищаются, имена файлов генерируются сервером. `send_from_directory` ограничивает путь, доступ к фотографиям архивных лотов закрывается SQL-фильтром. Декомпрессионные ошибки перехватываются. Это существенное сужение атакующей поверхности: посторонний пользователь не загружает файл в декодер через API.
- Docker: non-root UID 10001, read-only root filesystem, удалены capabilities, `no-new-privileges`, loopback-публикация порта; build context допускает только runtime-файлы. В CI actions закреплены SHA. Контейнер в этом подпроекте аудита не запускался.

## Находки

### SEC-01 — Контроль частоты не охватывает публичные фотографии и HTML, P2

**Статус:** отсутствие прикладного ограничения подтверждено прочитанным кодом и изолированной проверкой. Фактический DoS через внешний туннель не воспроизводился. При публичной публикации ссылки перед фиксированным дедлайном приоритет можно повысить до P1 после оценки edge.

**Где:** `throttle`, [app.py:268–304](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L268-L304), и `photo`, [app.py:490–506](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L490-L506). Частотные buckets вызываются только для `/api/`. HTML читает шаблон и выполняет рендер, фото делают запрос SQLite и файловую выдачу. Единственный web worker и uplink хоста общие для участников и гостей.

**Права:** не требуются ни Telegram, ни заявка, ни одобрение. URL фото является публичным по условиям продукта.

**Проверка:** временная SQLite/каталог, синтетический лот с JPEG 3×3, Flask test client без сети. 305 запросов `/api/lots` дали 300 ответов 200 и 5 ответов 429; следующие 305 запросов публичного фото дали 305 ответов 200. Это доказывает различие покрытия, не истощение ресурсов. Receipt: `security-probes.json`.

**Действие:** определить и воспроизводимо настроить внешние лимиты на весь публичный трафик (API, фото, HTML), размер тела/таймауты, разумный burst для одновременной загрузки нескольких изображений. Прикладной лимит фото может быть дополнительным рубежом, но не заменяет ограничение до занятого WSGI-потока. Не смешивать bucket организатора и анонимного клиента. Не ограничивать каталог настолько, чтобы легитимный polling/миниатюры вызывали ложные 429.

**Приёмка:** на изолированном HTTPS-прокси один клиент сверх выбранного лимита получает 429/ограничение до WSGI; параллельный синтетический организатор продолжает читать/писать; после окна доступ восстанавливается. Проверить 6 фото и polling на двух вкладках. Нагрузку живому аукциону не направлять. Документация Flask отдельно рекомендует лимиты на уровне ОС, WSGI/HTTP-сервера и платформы наряду с [MAX_CONTENT_LENGTH/form limits](https://flask.palletsprojects.com/en/stable/web-security/#resource-use).

### SEC-02 — Gunicorn 26.2.0 предшествует усилению HTTP/1-парсера, P2

**Статус:** используемая версия и поведение её парсера подтверждены; внешняя достижимость через Cloudflare не установлена. Это не подтверждённый удалённый захват или RCE.

**Где:** [requirements.txt:3](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/requirements.txt#L3). Native supervisor использует Gunicorn с 1 worker/4 threads; Docker — тот же runtime.

**Источник:** официальный [релиз 26.2.1](https://github.com/benoitc/gunicorn/releases/tag/26.2.1) от 5 сентября 2026 года ограничил накопление незавершающейся chunk-size строки и trailer section существующими request limits и изменил обработку malformed chunked body. [26.2.2](https://github.com/benoitc/gunicorn/releases/tag/26.2.2), 6 сентября, является следующим bug-fix релизом. Его отдельное исправление ASGI reuse не приписывается WSGI-приложению. HTTP/2 исправления также не объявляются достижимыми: h2c по умолчанию off, приложение не включает его.

**Проверка:** непосредственно `gunicorn.http.body.ChunkedReader` установленного 26.2.0, без сокетов, получает конечную строку chunk-size с расширением длиной 65 540 байт и принимает size=1. Парсер не применяет к этой строке ограничение длины. Нагрузка/неограниченная память не создавались. Receipt: `gunicorn-parser-probe.json`.

**Права/условия:** противник должен доставить соответствующее сырое HTTP/1 framing до Gunicorn. Прокси может разбирать и заменять framing, поэтому результат unit probe не доказывает такой путь через Cloudflare. Требуется отдельный изолированный тест topology.

**Действие и приёмка:** обновить закреплённый Gunicorn до проверенного поддерживаемого patch (на дату исследования 26.2.2), прогнать существующие проверки и изолированный HTTP/1 тест чрезмерных chunk extensions/trailers. Ожидается ограниченный 400/закрытие соединения, стабильная память и возможность параллельного штатного запроса. Не включать HTTP/2 ради этого исправления.

### SEC-03 — Полный runtime-набор Python не закреплён, аудит зависимостей не включён в CI, P2

**Статус:** подтверждено manifest, CI и двумя свежими инвентаризациями. Компрометация supply chain не обнаружена.

**Где:** четыре прямые версии в [requirements.txt](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/requirements.txt); install без hash checking в [Dockerfile](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/Dockerfile); `pip-audit` установлен среди dev tools, но [.github/workflows/checks.yml](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/.github/workflows/checks.yml) его не запускает.

**Доказательство:** свежий resolver requirements выбрал Click 8.5.1, локальный venv содержит 8.5.0; остальные проверенные runtime-пакеты совпали. Таким образом одинаковые tag/commit и четыре прямых pin не задают идентичное runtime-окружение. Версии подзависимостей и wheel/native codecs могут изменяться между сборками. Python-интерпретатор и платформенные wheels также являются частью идентичности; это не утверждение о детерминизме всей ОС.

**Действие:** подготовить reviewable lock/constraints для runtime и hashes допустимых wheels под поддерживаемые платформы, явно управлять обновлением. Отдельный CI-шаг `pip-audit` для закреплённого runtime сохраняет JSON и блокирует новые релевантные advisory. Для релиза сохранять resolved inventory, SHA commit, digest контейнера/пакета. Не публиковать токены, usernames или пути личного хранилища. [pip документирует pin всех зависимостей и hash checking](https://pip.pypa.io/en/stable/topics/secure-installs/).

**Приёмка:** два чистых install для одной целевой платформы имеют одинаковые package/version/hashes; установка неожиданного артефакта отклоняется; CI-аудит реально вызывается и сохраняет отчёт. Существующие Python/frontend/container smoke проверки проходят.

## Проверка advisory и версии

Оба реально выполненных аудита завершились успешно с `vulns=[]`:

1. `.venv/bin/pip-audit -r requirements.txt --format json --output /tmp/friends-auction-audit-20261124/pip-audit-requirements.json` — свежая резолюция runtime, 10 пакетов.
2. Из установленных metadata сформирован `installed-runtime.txt` для тех же 10 runtime-пакетов; `.venv/bin/pip-audit --no-deps --disable-pip -r /tmp/friends-auction-audit-20261124/installed-runtime.txt --format json --output /tmp/friends-auction-audit-20261124/pip-audit-installed-runtime.json` — фактический локальный venv, не инвентаризация работающего deployment.

Первый вызов с output в ещё не созданном каталоге сообщил отсутствие известных advisory, затем завершился ошибкой записи. Он не учитывается как успешный: после создания каталога вызов повторён, JSON успешно записан и прочитан. Пакеты, секреты и working tree не изменялись. Для второго вызова предупреждение `--no-deps` ожидаемо: файл получен из полного runtime-дерева свежей резолюции, затем версии сверены с metadata; dev-среда целиком не аудировалась.

| Компонент              | Зафиксированная/проверенная версия                                                                      | Разбор релевантности                                                                                                                                                                                                                                                                                                               |
| ---------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Flask                  | 3.1.3                                                                                                   | [Upstream changes](https://flask.palletsprojects.com/en/stable/changes/) связывает 3.1.3 с исправлением session-access/caching. Снимок использует эту версию и не строит авторизацию на Flask session. Найденный класс session advisory не следует переносить на initData.                                                         |
| Pillow                 | 13.0.0                                                                                                  | [Официальное GHSA-wqgw-9xfh-qf2g](https://github.com/python-pillow/Pillow/security/advisories/GHSA-wqgw-9xfh-qf2g) указывает affected <=12.3.0, patched 13.0.0. Важное различие: EXIF-парсинг reachable и для JPEG/PNG, поэтому одно ограничение пикселей не было бы защитой на старой версии. Текущий pin содержит исправление.   |
| Pillow FPX/JPEG        | 13.0.0                                                                                                  | [Официальное GHSA-5x8w-8jj5-q52h](https://github.com/python-pillow/Pillow/security/advisories/GHSA-5x8w-8jj5-q52h) также исправлено в 13.0.0. Advisory требует специфического формата/optional `olefile`; их наличие не предполагается. Атаку не воспроизводили.                                                                   |
| Gunicorn               | 26.2.0                                                                                                  | Известных записей по результату pip-audit нет; более позднее upstream hardening всё равно релевантно, см. SEC-02. Отсутствие GHSA не заменяет changelog.                                                                                                                                                                           |
| pillow-heif            | 1.8.1                                                                                                   | [Upstream release](https://github.com/bigcat88/pillow_heif/releases/tag/v1.8.1) обновляет libheif до 1.23.6; версия 1.7.0 ранее исправила encode out-of-bounds read. Приложение декодирует HEIF и сохраняет через Pillow JPEG, а не кодирует HEIF. Native-зависимости конкретного deployment binary отдельно не инвентаризированы. |
| Werkzeug/Jinja2/прочее | Werkzeug 3.1.9, Jinja2 3.1.6, MarkupSafe 3.0.4, ItsDangerous 2.2.0, Blinker 1.9.0, Click 8.5.0 локально | `pip-audit` не нашёл известных записей; Windows-only safe_join advisory не объявляется атакой на macOS/Linux. [Upstream Werkzeug advisory index](https://github.com/pallets/werkzeug/security/advisories) проверен. Не применяется PyJWT: проект его не использует.                                                                |

Этот результат означает отсутствие известных записей в использованном advisory-сервисе для перечисленных версий на дату проверки. Он не охватывает нераскрытые дефекты, malicious packages, всю ОС, Python/OpenSSL и все bundled native libraries. Так ограничивает область и сам [pip-audit](https://github.com/pypa/pip-audit#known-limitations).

## Публичность, лицензия и идентичность релиза

Отдельное P3 улучшение прозрачности: [README:257–259](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/README.md#L257-L259) честно сообщает отсутствие LICENSE. Это публичный исходный код, однако разрешение свободно использовать/изменять/распространять его не оформлено. Если цель — именно open source, владелец выбирает подходящую лицензию; автоматически делать юридический выбор за него не следует. [GitHub поясняет различие публичного репозитория и лицензированного open source](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

`/api/version` возвращает только `__version__`, [app.py:526–529](https://github.com/PaVeLlLlLX/friends-auction/blob/45ccee95610328be1bdb23e5ae81325b6540f4d6/auction/app.py#L526-L529). Это диагностическая декларация, её можно вернуть и из изменённого сервера. Публичный tag, CI и digest/SBOM позволяют сопоставить подготовленный артефакт с исходниками, но обычный endpoint не доказывает посетителю честность оператора или идентичность исполняемого процесса. Для масштаба проекта достаточно immutable release directory, сохранённого resolved inventory/digest и честной формулировки. Доверенный runtime attestation не предлагается.

## Допустимые компромиссы и открытые проверки

Повторный приём одного initData в пределах 24 часов воспроизведён: два одинаковых admin-запроса дают 200; проверка подписи принимает age 23h и отвергает 25h. Это ожидаемая текущая аутентификационная модель, не обход подписи. Сократить окно именно для organizer действий и обменять initData на короткую серверную сессию можно как отдельное P3 улучшение, предварительно решив обновление авторизации долго открытого Mini App. Не вводить одноразовость всех initData вслепую: обычные повторные API-запросы одного WebView тогда сломаются.

Для будущих изменений полезно ограничить `Image.open(..., formats=...)` ещё до auto-detection, а декодирование при расширении аудитории переносить в процесс с лимитом памяти/времени. Сейчас это защита в глубину, а не подтверждённая RCE: загрузка закрыта организатором, pin Pillow обновлён, конкретный вредоносный decode path не найден.

Не проверялись: реальные Telegram initData на физических клиентах; полный набор cookie/WebView/iframe политик; внешний Cloudflare framing и edge limits; компрометация Telegram/ОС; доступность при DDoS; привязка работающего сервиса к commit; полная история Git на когда-либо попадавшие секреты; политика GitHub secret scanning/branch protection; фактические права файлов живого deployment. Для рекомендаций выше эти неизвестные обозначены явно, не подменены предположением о безопасности.

## Реестр основных источников

Все внешние страницы открыты 24.11.2026; это источники для конкретных утверждений, а не квота полноты.

| Организация/проект     | Материал и версия/дата                                                                                                                                               | Поддерживает                            |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| Telegram               | [Mini Apps, Validating data](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app), текущая документация                                 | HMAC, срок initData и границы подписи   |
| Pallets                | [Flask Security](https://flask.palletsprojects.com/en/stable/web-security/),3.1.x                                                                                    | Resource limits, CSRF/XSS модель        |
| Pallets                | [Flask Changes](https://flask.palletsprojects.com/en/stable/changes/),3.1.3,18.02.2026                                                                               | Фактическое session-исправление         |
| Pillow maintainers     | [GHSA-wqgw-9xfh-qf2g](https://github.com/python-pillow/Pillow/security/advisories/GHSA-wqgw-9xfh-qf2g),02.11.2026                                                    | EXIF/TIFF amplification, affected/patch |
| Pillow maintainers     | [GHSA-5x8w-8jj5-q52h](https://github.com/python-pillow/Pillow/security/advisories/GHSA-5x8w-8jj5-q52h),17.10.2026                                                    | FPX предпосылки, patch13.0.0            |
| Gunicorn maintainers   | [26.2.1 release](https://github.com/benoitc/gunicorn/releases/tag/26.2.1),05.09.2026                                                                                 | HTTP/1 и отдельное HTTP/2 hardening     |
| Gunicorn maintainers   | [26.2.2 release](https://github.com/benoitc/gunicorn/releases/tag/26.2.2),06.09.2026                                                                                 | Последующий bug-fix patch               |
| pillow-heif maintainer | [v1.8.1 release](https://github.com/bigcat88/pillow_heif/releases/tag/v1.8.1),11.10.2026                                                                             | Native libheif версия                   |
| Pallets                | [Werkzeug security advisories](https://github.com/pallets/werkzeug/security/advisories)                                                                              | Проверка названий/Windows scope         |
| PyPA                   | [pip Secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/),26.3                                                                                    | Hash checking и полные pins             |
| PyPA                   | [pip-audit README](https://github.com/pypa/pip-audit#known-limitations)                                                                                              | Область advisory scan и ограничения     |
| GitHub                 | [Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) | Публичность и лицензия                  |

## Артефакты выполненной проверки

- `security-probes.json`: временный app, роли, подпись, rate-limit coverage, replay и headers.
- `gunicorn-parser-probe.json`: ограниченный unit probe парсера без сокетов.
- `pip-audit-requirements.json`: свежая резолюция 10 runtime-пакетов, 0 известных записей.
- `installed-runtime.txt` и `pip-audit-installed-runtime.json`: локальные runtime-версии и их advisory scan.

JSON-протоколы сохранены в [evidence](evidence/); временные драйверы проверок остались вне репозитория в `/tmp/friends-auction-audit-20261124/`. Общий pytest и прочие проектные проверки запускаются главным агентом отдельно и в этот подпроект отчёта не засчитываются без его результатов.
