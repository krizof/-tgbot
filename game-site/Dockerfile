FROM node:22-alpine AS dependencies
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM node:22-alpine AS runner
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
RUN addgroup -S game && adduser -S game -G game
COPY --from=builder --chown=game:game /app/public ./public
COPY --from=builder --chown=game:game /app/.next/standalone ./
COPY --from=builder --chown=game:game /app/.next/static ./.next/static
USER game
EXPOSE 3000
CMD ["node", "server.js"]
