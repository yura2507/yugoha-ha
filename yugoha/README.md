# yuGoHA

Сервер уведомлений для Home Assistant и Android yuGoHA.

## Одна установка

Пользователь устанавливает только **yuGoHA App** из магазина Home Assistant.
App сам:
1. устанавливает/обновляет bundled custom integration `yugoha`;
2. публикует discovery через Supervisor;
3. один раз перезапускает Home Assistant Core после изменения integration;
4. создаёт интеграцию автоматически через `hassio` discovery;
5. предоставляет действие `yugoha.send`.

После этого отдельная установка через HACS, SSH или копирование `custom_components`
не требуется.

## Архитектура

Home Assistant → `yugoha.send` → yuGoHA App → Firebase пользователя → FCM → Android

При доступности локальной сети Android также использует HTTP/WebSocket к yuGoHA App
для истории, удаления, read-состояния и синхронизации.

Белый IP не требуется. При CGNAT новые сообщения приходят через FCM, локальные
read/delete синхронизируются при возвращении телефона в домашнюю сеть.

## Автор

**yura2507**

Дзен: https://dzen.ru/yura2507
