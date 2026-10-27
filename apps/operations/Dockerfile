# Management API + dashboard image. Build from the drproj-mgmt repo root:
#   docker build -f apps/operations/Dockerfile -t drproj-operations:local .
# Local build only in P3; publishing images is a later, explicit step.
ARG NODE_IMAGE=node:22.23.4-alpine

FROM ${NODE_IMAGE} AS base
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc tsconfig.base.json ./
COPY apps/operations/package.json apps/operations/
COPY packages/contracts/package.json packages/contracts/

FROM base AS build
RUN pnpm install --frozen-lockfile --filter "@drproj/operations..."
COPY packages/contracts packages/contracts
COPY apps/operations apps/operations
RUN pnpm --filter @drproj/operations run build:web

FROM base AS prod-deps
RUN pnpm install --frozen-lockfile --prod --filter "@drproj/operations..."

FROM ${NODE_IMAGE}
ENV NODE_ENV=production
WORKDIR /app
COPY --from=prod-deps --chown=node:node /app/node_modules ./node_modules
COPY --from=prod-deps --chown=node:node /app/apps/operations/node_modules ./apps/operations/node_modules
COPY --from=prod-deps --chown=node:node /app/packages/contracts/node_modules ./packages/contracts/node_modules
COPY --chown=node:node package.json tsconfig.base.json ./
COPY --chown=node:node packages/contracts packages/contracts
COPY --chown=node:node apps/operations/package.json apps/operations/tsconfig.json apps/operations/
COPY --chown=node:node apps/operations/src apps/operations/src
COPY --chown=node:node apps/operations/scripts apps/operations/scripts
COPY --from=build --chown=node:node /app/apps/operations/web/dist apps/operations/web/dist
USER node
WORKDIR /app/apps/operations
# Plain HTTP on the Compose network only (Caddy terminates TLS); /metrics (9465) is internal-only.
EXPOSE 8080 9465
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD ["node", "-e", "require('http').get({host:'127.0.0.1',port:process.env.OPS_PORT||8080,path:'/health/live'},r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"]
CMD ["node", "--import", "tsx", "src/main.ts"]
