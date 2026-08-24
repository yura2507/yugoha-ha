# Установка yuGoHA

1. В Home Assistant откройте Магазин приложений.
2. Добавьте репозиторий:
   `https://github.com/yura2507/yugoha-ha`
3. Установите App `yuGoHA`.
4. Запустите App.
5. Во время первого запуска App автоматически установит интеграцию `yugoha` и
   один раз перезапустит Home Assistant Core.
6. Откройте Web UI yuGoHA.
7. Загрузите `service-account.json` своего Firebase project.
8. На Android откройте `Настройки → yuGoHA Server`, укажите локальный адрес HA,
   код сопряжения и сопрягите телефон.

После этого в автоматизациях доступно:

```yaml
action: yugoha.send
data:
  message: "1.info. Проверка yuGoHA"
  title: "Home Assistant"
  priority: 5
```

При сером IP внешний адрес Android можно оставить пустым.

Автор: yura2507
Дзен: https://dzen.ru/yura2507
