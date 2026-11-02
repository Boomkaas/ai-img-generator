FROM node:26 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.30-alpine

RUN apk upgrade --no-cache && rm /etc/nginx/conf.d/default.conf
COPY infra/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend /frontend/dist /srv/frontend

USER nginx
EXPOSE 8080 8443
ENTRYPOINT []
CMD ["nginx", "-g", "daemon off;"]
