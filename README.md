# honor-fingerprint-linux

Сканер отпечатков **Egis ETU906 (USB `1c7a:05aa`)** в Linux — тот, что встроен в кнопку питания ноутбуков Honor MagicBook (проверено на HONOR MRB, Debian 13 trixie, GNOME).

*Fingerprint reader support for the Egis ETU906 (1c7a:05aa) on Linux: a patched libfprint with SDCP support plus a GTK 4 manager app, packaged as two `.deb` files.*

Upstream libfprint этот сенсор не поддерживает: он работает только по защищённому протоколу SDCP. Здесь — форк libfprint с SDCP ([obra/libfprint-egismoc-sdcp](https://github.com/obra/libfprint-egismoc-sdcp), на основе работы Joshua Grisham) с исправлениями для ETU906 и приложение для управления отпечатками.

## Что работает

- запись, проверка и удаление отпечатков (fprintd, 15 касаний на палец);
- вход в GNOME и разблокировка экрана пальцем;
- sudo, вход в консоль и окна авторизации по пальцу (PAM), с откатом на пароль;
- блокировка экрана кнопкой питания со сканером (как Super+L).

## Установка

Скачайте два пакета со страницы [Releases](../../releases) и установите двойным щелчком (откроется «Центр приложений»), сначала драйвер, потом приложение:

| Пакет | Что внутри |
|---|---|
| `libfprint-egis-sdcp_*_amd64.deb` | Драйвер: libfprint с SDCP и поддержкой 05aa в `/opt/libfprint-egis-sdcp`. fprintd подхватывает его вместо libfprint из Debian. |
| `fprint-manager_*_all.deb` | Приложение «Отпечатки пальцев». Работает и с другими сканерами, которые поддерживает fprintd. |

Или из терминала:

```bash
sudo apt install ./libfprint-egis-sdcp_*_amd64.deb ./fprint-manager_*_all.deb
```

Потом откройте **«Отпечатки пальцев»** в меню приложений и запишите пальцы.

> **Отпечатки Windows Hello.** Сенсор хранит отпечатки у себя в чипе. Если палец уже записан в Windows, запись в Linux скажет «Этот палец уже записан». Либо используйте другие пальцы, либо очистите память сенсора (вкладка «Драйвер» → «Очистить память сенсора») — тогда Windows Hello придётся настроить заново.

## Приложение «Отпечатки пальцев»

- **Пальцы** — обе руки, запись с прогрессом и подсказками, удаление, проверка.
- **Вход в систему** — вход и sudo по пальцу (PAM), блокировка экрана кнопкой питания.
- **Драйвер** — сенсор, какая libfprint используется, состояние fprintd, перезапуск, журнал, отладочный лог, очистка памяти сенсора.

Действия администратора выполняет помощник `/usr/libexec/fprint-manager-helper` через polkit; он принимает только фиксированный набор команд.

## Что исправлено в драйвере

Все правки — в [`patches/egis-05aa.patch`](patches/egis-05aa.patch) (относительно форка `obra/libfprint-egismoc-sdcp`), в каталоге `driver/` они уже применены.

1. **ID 05aa** добавлен в драйвер `egismoc` (тип TYPE2, 15 касаний).
2. **Инициализация.** ETU906 отвергает служебные запросы `0x20` (stall); драйвер Windows шлёт только `0x52` — лишние шаги для 05aa пропускаются.
3. **SDCP-кэш.** Сенсор помнит только последнюю SDCP-сессию, поэтому закэшированный на диске ключ быстро устаревает (другой процесс, Windows, сон) и проверка падает с `AuthorizedIdentity verification failed`. Теперь при каждом запуске устанавливается новое соединение (~80 мс).
4. **Падение fprintd после отмены.** Отмена проверки оставляла «висящую» задачу, и следующий запрос к сенсору завершал fprintd по `assert (task_ssm == NULL)`. Исправлено.
5. **Сборка.** `egismoc` теперь явно линкуется с OpenSSL.

## Сборка пакетов

```bash
sudo apt install meson ninja-build pkg-config gcc libglib2.0-dev libgusb-dev libgudev-1.0-dev \
    libjson-glib-dev libnss3-dev libpixman-1-dev libcairo2-dev libssl-dev libudev-dev systemd-dev dpkg-dev
./packaging/build-deb.sh [версия-драйвера] [версия-приложения]
```

Пакеты появятся в `dist/`.

## Удаление

```bash
sudo apt remove fprint-manager libfprint-egis-sdcp
```

После удаления драйвера fprintd вернётся к libfprint из Debian, а с ней ETU906 не работает.

## Отладка

Включите «Отладочный журнал» на вкладке «Драйвер» и посмотрите «Журнал службы». Полезная особенность ETU906: interrupt-endpoint `0x83` выдаёт текстовый отладочный лог прошивки (пакеты `elog`) — с ним видно, что сенсор делает на самом деле.

## Лицензия

LGPL-2.1-or-later, как и libfprint — см. [LICENSE](LICENSE).
