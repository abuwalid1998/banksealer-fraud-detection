# syntax=docker/dockerfile:1
# Build the React console, then serve it with nginx (which also proxies /api to the api service)
FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
# Optional build secret "corporate_ca": extra CA for networks with TLS inspection (empty by default).
RUN --mount=type=secret,id=corporate_ca,required=false \
    if [ -s /run/secrets/corporate_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/corporate_ca; fi; \
    npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
