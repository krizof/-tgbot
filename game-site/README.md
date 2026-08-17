# Friends Arcade — Telegram Mini App

Мобильная игровая страница для закрытого Telegram-бота. Включает «Змейку» и десятисекундный «Тап-спринт».

```bash
pnpm install
pnpm dev
pnpm build
```

Результат отправляется через `Telegram.WebApp.sendData`, поэтому страницу нужно открывать только кнопкой `KeyboardButton.web_app`, которую бот показывает после `/games`. На странице нет токена бота, Telegram ID или базы участников. Результат принимает бот и хранит в своей SQLite-базе.

При push в `main` workflow `pages.yml` собирает статическую версию и публикует её по адресу `https://krizof.github.io/-tgbot/`. Бот продолжает работать локально в Docker и получает этот адрес через `MINI_APP_URL`.
