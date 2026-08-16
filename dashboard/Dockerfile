# =============================================================================
#  SPC Dashboard (Next.js 14) — 三階段建置
#  ---------------------------------------------------------------------------
#  build:  docker build -t spc-dashboard .
#  run:    docker run -p 3000:3000 --env-file .env spc-dashboard
#
#  這個 image 不含資料庫，也不含 Python SPC service。兩者都靠執行時的環境
#  變數指過去(DATABASE_URL / SPC_API_BASE)，所以同一個 image 可以在任何
#  環境重複使用，不需要重 build。
# =============================================================================

# --- 階段 1：只裝相依套件 -----------------------------------------------------
# 單獨切一層是為了 layer cache：只要 package-lock.json 沒變，
# 改再多原始碼都不會重跑 npm ci。
FROM node:20-alpine AS deps
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci

# --- 階段 2：編譯 -------------------------------------------------------------
FROM node:20-alpine AS builder
WORKDIR /srv
COPY --from=deps /srv/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# 這一步不需要連 DB，也不需要 Python 服務：
# app/page.tsx 是 "use client"，且全部 API route 都標了 force-dynamic，
# 所以 next build 不會在建置期去抓資料。
RUN npm run build

# --- 階段 3：最終 image -------------------------------------------------------
# 編譯器、TypeScript、完整 node_modules 都留在上面兩層，不會進到這裡。
FROM node:20-alpine AS runner
WORKDIR /srv

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
# standalone 的 server.js 是讀 HOSTNAME 決定綁哪個介面。
# 不設的話某些版本會綁 localhost，容器外面(包含其他容器)就連不進來。
ENV HOSTNAME=0.0.0.0

# 不用 root 跑應用程式。
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs

# standalone 目錄已含 server.js 與被 tracing 挑出來的最小 node_modules(含 pg)。
COPY --from=builder --chown=nextjs:nodejs /srv/.next/standalone ./
# 靜態資源不會被 standalone 打包，要另外複製。
COPY --from=builder --chown=nextjs:nodejs /srv/.next/static ./.next/static

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
