# yuGoHA

Репозиторий Home Assistant App для **yuGoHA 0.4.5**.

yuGoHA — локальная система уведомлений для Home Assistant и Android. Пользователь устанавливает только Home Assistant App `yuGoHA`; интеграция `yugoha` устанавливается и настраивается автоматически.

## Установка

Добавьте репозиторий в Home Assistant:

`https://github.com/yura2507/yugoha-ha`

Затем установите App **yuGoHA** из магазина приложений.

> Важно: при первом запуске yuGoHA автоматически устанавливает интеграцию и один раз перезапускает Home Assistant Core.

После установки в автоматизациях доступно действие:

```yaml
action: yugoha.send
data:
  message: "1.info. Проверка yuGoHA"
```

Новые сообщения доставляются через FCM, а при доступности локальной сети Android использует HTTP/WebSocket для истории, прочтения, удаления и синхронизации. Белый IP не требуется.

## Автор

© 2026 **yura2507**  
Дзен: https://dzen.ru/yura2507
