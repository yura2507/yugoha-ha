# 0.4.0

- Первый GitHub-ready выпуск.
- Пользователь устанавливает только Home Assistant App `yuGoHA`.
- App автоматически копирует bundled custom integration `yugoha` в `custom_components`.
- App регистрирует Supervisor discovery для автоматического создания config entry.
- Home Assistant Core перезапускается один раз только при новой версии integration.
- Сохраняется действие `yugoha.send`.
- Добавлено авторство `yura2507` и ссылка на Дзен: https://dzen.ru/yura2507
- Сервер сообщений, WebSocket, FCM, локальная SQLite и CGNAT-сценарий сохранены.
