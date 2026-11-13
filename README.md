# Throne VPN — плагин Decky для Steam Deck

VPN в игровом режиме Steam Deck через [Throne](https://github.com/throneproj/Throne)
(ядро sing-box: VLESS/Reality, XHTTP, Hysteria2, Trojan, Shadowsocks, WireGuard и др.).

## Установка (режим рабочего стола)

Откройте Konsole и выполните:

```bash
curl -L -o ~/throne-deck-install.sh https://github.com/solidkeffir/decky-throne/releases/latest/download/throne-deck-install.sh
bash ~/throne-deck-install.sh
```

Скрипт сам:

1. попросит пароль `sudo` (если у deck его нет — предложит задать);
2. поставит [Decky Loader](https://decky.xyz) официальным установщиком, если его ещё нет;
3. скачает последний Throne для Linux с GitHub и сверит SHA-256 с релизом
   (если GitHub недоступен — возьмёт `Throne-*-linux-amd64.zip` из «Загрузок»);
4. поставит плагин «Throne VPN» (он вшит в скрипт);
5. добавит ярлык «Throne» в меню и на рабочий стол и откроет окно Throne.

В окне Throne: скопируйте ссылку на подписку → `Ctrl+V` → «Создать новую группу подписки»
→ OK. Закройте Throne через «Программа» → «Выход». Дальше — игровой режим: «…» → Decky →
«Throne VPN» → включить VPN.

Повторный запуск скрипта обновляет Throne и плагин. Удаление: `bash ~/throne-deck-install.sh --uninstall`
(профили остаются в `~/.config/Throne`).

## Как это устроено

- Throne лежит в `~/homebrew/data/decky-throne/throne/` (владелец root).
- Плагин запускает Throne в **headless-режиме** (Throne 1.4+) службой `decky-throne.service`
  от пользователя deck с сетевыми capabilities — так, как предлагает сам Throne
  (`dist/linux/caps.conf`). TUN работает без setuid и sudo.
- Данные — `~/.config/Throne`, общие с окном Throne: что добавили в окне, видно в игровом режиме.
  Окно и служба не работают одновременно: ярлык «Throne» останавливает службу, а после
  закрытия окна запускает её снова (правило polkit `49-decky-throne.rules` разрешает deck
  это без пароля). Если окно закрылось вместе с режимом рабочего стола, службу через ~15 с
  запустит сам плагин.
- Плагин управляет Throne через **Throne API** (`127.0.0.1:9096`): шифрованный протокол
  ChaCha20-Poly1305 реализован на чистом Python (`py_modules/throne_api.py`) и проверен
  официальными тестовыми векторами.
- При перезапуске Decky Throne не останавливается, VPN не отключается.

## Что умеет панель в игровом режиме

- Включить/выключить VPN, текущий сервер, пинг и скорость.
- Подписки: выбор, обновление, удаление; серверы с пингом; «Лучший сервер».
- Добавить подписку или ссылки `vless://`, `ss://`, `trojan://`, `hy2://`
  (в игровом режиме нет вставки: «Вставить» берёт текст из `~/throne.txt`, если буфер недоступен).
- Правила маршрутизации из подписки (формат Happ), выбор профиля маршрутизации.
- TUN или только прокси `127.0.0.1:2080`, автоподключение при включении Deck, журнал,
  перезапуск и обновление Throne. Если Throne не установлен, панель поставит его сама.

## Сборка

```bash
npm install
./build.sh        # → out/throne-deck-install.sh (плагин внутри)
```

`out/throne-deck-install.sh` выкладывается в релиз на GitHub — это единственный файл,
который нужен пользователю.

Тесты: `python3 tests/test_protocol.py` (векторы протокола) и `tests/smoke_fake.py`
против `fakethrone` из [Throne-api](https://github.com/throneproj/Throne-api).
