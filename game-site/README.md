# Friends Arcade — Telegram Mini App

Мобильная игровая страница для закрытого Telegram-бота. Включает «Змейку» и десятисекундный «Тап-спринт».

```bash
pnpm install
pnpm dev
pnpm build
docker build -t friends-arcade .
```

Результат отправляется через `Telegram.WebApp.sendData`, поэтому страницу нужно открывать только кнопкой `KeyboardButton.web_app`, которую бот показывает после `/games`. На странице нет токена бота, Telegram ID или базы участников. Результат принимает бот и хранит в своей SQLite-базе.

В production страницу запускает `compose.prod.yaml` основного проекта, а Caddy автоматически выдаёт HTTPS-сертификат для `GAME_DOMAIN`.
