# Защита маржи — Telegram-бот для Wildberries

Бот периодически читает карточки и цены через официальный API Wildberries (только методы чтения), рассчитывает маржу по настроенным расходам и отправляет сводное уведомление при переходе маржи ниже порога. Цены, скидки, карточки и остатки бот не изменяет.

## Архитектура (Clean Architecture)

Проект разделён на строго изолированные слои с однонаправленными зависимостями внутрь (Dependency Rule):

```text
src/margin_guard/
├── domain/             # Сущности, Value Objects, чистые формулы маржи и правила переходов (0 внешних зависимостей)
├── application/        # Порты (Protocol), DTO и сценарии использования (Use Cases)
├── infrastructure/     # Адаптеры SQLite (WAL, 0600), MultiFernet, Rate Limiter, фильтр логов, HTTP-клиент WB
├── presentation/       # Тонкий Telegram-адаптер на базе Aiogram v3 и чистые форматтеры сообщений
├── config.py           # Неизменяемая конфигурация (Pydantic Settings + SecretStr)
└── bootstrap.py        # Корень композиции (Composition Root)
```

Соблюдение границ слоёв автоматически проверяется архитектурными тестами (`tests/test_architecture.py`).

## Возможности и механизмы безопасности

- **Контекстное шифрование и ротация ключей:** токены WB шифруются через `MultiFernet` с криптографической привязкой шифротекста к `telegram_id` владельца и поддержкой ротации ключей без простоя (`TOKEN_ENCRYPTION_KEY=NEW_KEY,OLD_KEY`).
- **Защита секретов в чате:** `/connect` удаляет сообщение до сохранения токена, перехватывает отредактированные сообщения (`edited_message`) и предупреждает о необходимости отзыва при случайной отправке токена в групповой чат.
- **Контроль доступа и защита от DoS:** опциональный белый список `ALLOWED_TELEGRAM_IDS`, скользящий rate limiter с ограниченной памятью, потоковый лимит размера HTTP-ответа (`10 MiB`), лимит пагинации (`200` страниц), защита от `Decimal`-экспонент (`1e99999999`) и изоляция сбоев по каждому продавцу в фоновом цикле.
- **Гигиена данных и контейнера:** права `0700`/`0600` на каталог и БД SQLite, минимизация кэшируемых JSON-данных, фильтрация секретов и CRLF в логах (`RedactingLogFilter`), multi-stage Docker-образ с кодом в режиме только для чтения.

Подробное описание модели угроз и ротации ключей приведено в `SECURITY.md`.

## Быстрый старт локально

1. Скопируйте файл конфигурации и ограничьте права доступа:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

2. Заполните `TELEGRAM_BOT_TOKEN` и сгенерируйте ключ шифрования:

   ```sh
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Вставьте сгенерированное значение в `TOKEN_ENCRYPTION_KEY`. При необходимости ограничьте доступ к боту списком своих Telegram ID в `ALLOWED_TELEGRAM_IDS`.

3. Создайте виртуальное окружение, установите зависимости и запустите проверки качества:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -e ".[dev]"

   ruff check src tests
   mypy src
   bandit -r src
   pytest --cov=margin_guard
   ```

4. Запустите бота:

   ```sh
   python -m margin_guard
   ```

## Команды бота

- `/start` — регистрация профиля и краткая справка;
- `/connect <токен>` — безопасное подключение токена WB (только чтение: «Контент» и «Цены и скидки»);
- `/products` — синхронизация карточек и цен из WB;
- `/cost <артикул> <сумма>` — установка себестоимости единицы товара;
- `/fees default|артикул категория=значение ...` — настройка общих или индивидуальных расходов (`commission`, `logistics`, `tax`, `storage`, `advertising`);
- `/threshold <процент>` — порог маржи для оповещений;
- `/status` — сводка настроек и состояния мониторинга;
- `/pause` / `/resume` — приостановка и возобновление проверок;
- `/delete_me` — полное удаление токена и кэша товаров пользователя.

## Запуск в Docker (Hardened)

```sh
docker build -t margin-guard-bot .
mkdir -p data && chmod 700 data
docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  margin-guard-bot
```
