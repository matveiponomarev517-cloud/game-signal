# Игровой сигнал: автономный канал

Каждый час GitHub Actions запускает `bot.py`. Если в окне 9–23 МСК не исчерпан лимит (5 постов в сутки) и прошла пауза 2 часа, бот публикует следующий формат по кругу: новость → находка из Steam → «горячее» с Reddit → новость → лонгрид → опрос → новость. По воскресеньям лонгрид = «итоги недели». Иначе запуск заканчивается за секунды.

Новости берутся из 6 RSS-лент и через раз ищутся по всему интернету (поиск Google через Gemini; каждую страницу бот открывает и проверяет). Текст пишется по полному тексту страницы. Лонгрид выходит ответом на пост-анонс, так его забирает бот Дзена (подключите Telegram-канал в Студии Дзена, как в «Завтра ближе»).

## Секреты (Settings → Secrets and variables → Actions)
Обязательно: `TG_TOKEN`, `TG_CHAT_ID` (`@канал`), бот админ канала. Нейросеть: `GEMINI_API_KEY` (aistudio.google.com), запасной `GROQ_API_KEY`. Рекомендуется `TG_ADMIN_ID` (бот напишет, если что-то сломалось или 30 ч нет постов; сначала напишите боту /start).
Settings → Actions → General → Workflow permissions → Read and write.

## Настройки (Variables / env)
`MAX_PER_DAY` (5), `MIN_GAP_MINUTES` (120), `POST_HOUR_FROM`/`POST_HOUR_TO` (9/23), `WEB_SCOUT=0` (без поиска), `LONGREADS=0`, `SILENCE_ALERT_HOURS` (30), `GEMINI_MODEL` (авто), `CHANNEL_NAME`. Порядок форматов: `ROTATION` в `bot.py`.

## Проверка
Actions → channel → Run workflow (поле force: `news` / `gem` / `hot` / `longread` / `poll`). Ручной запуск обходит окно и паузу, но не дневной лимит.
Локально: `python bot.py --check`, `python bot.py --dry-run --force news`, `python test_offline.py`.
