FROM node:20-slim AS build
WORKDIR /app
# Browser-facing origins (baked at build). Defaults suit a host user at localhost.
ARG VITE_OKTA=http://localhost:8081/oauth2/default
ARG VITE_RUNTIME=http://localhost:8080
ENV VITE_OKTA=$VITE_OKTA VITE_RUNTIME=$VITE_RUNTIME
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
