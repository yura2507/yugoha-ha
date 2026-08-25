# 0.4.5

- Исправлена передача реального API key в Home Assistant integration через discovery.
- Исправлена ошибка `401 Unauthorized` при вызове `yugoha.send`.
- Интеграция автоматически устанавливается и обновляется из Home Assistant App.
- Добавлен повторный discovery/rediscovery после удаления или переустановки интеграции.
- Исправлена последовательность запуска: API yuGoHA поднимается до discovery.
- Добавлены глобально уникальные ID сообщений для защиты от коллизий после переустановки сервера.
- Сохраняется действие `yugoha.send` без изменения существующих автоматизаций.
- Поддерживаются FCM, локальный HTTP/WebSocket, история, read/delete и работа при сером IP/CGNAT.
- Добавлено предупреждение о единственном автоматическом перезапуске Home Assistant Core при первой установке интеграции.
- Автор: © 2026 yura2507 — https://dzen.ru/yura2507

# 0.4.0

- Первый GitHub-ready выпуск.
- Пользователь устанавливает только Home Assistant App `yuGoHA`.
- App автоматически устанавливает custom integration `yugoha` в `custom_components`.
- Добавлен Supervisor discovery.
- Добавлено действие `yugoha.send`.
- Сервер сообщений, WebSocket, FCM, локальная SQLite и CGNAT-сценарий сохранены.
