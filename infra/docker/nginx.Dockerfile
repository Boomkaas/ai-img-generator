FROM node:26 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM nginx:1.30-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY infra/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend /frontend/dist /srv/frontend

USER nginx
EXPOSE 8080 8443
# The image's entrypoint scripts rewrite files under /etc/nginx, which the nginx user can't write.
ENTRYPOINT []
CMD ["nginx", "-g", "daemon off;"]
