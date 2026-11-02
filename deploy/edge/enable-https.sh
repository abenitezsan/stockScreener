#!/bin/sh
# Activa HTTPS en el proxy con un certificado gratuito de Let's Encrypt.
#
#   ./enable-https.sh [DOMINIO] [EMAIL]
#
# Sin dominio propio se usa <ip-con-guiones>.sslip.io, un nombre público que resuelve a la IP
# del VPS (152.228.237.219 -> 152-228-237-219.sslip.io). Requisitos: los puertos 80 y 443
# abiertos y el proxy ya arrancado (docker compose up -d). Se puede repetir sin problema.
set -eu
cd "$(dirname "$0")"

IP=${PUBLIC_IP:-152.228.237.219}
DOMAIN=${1:-$(echo "$IP" | tr . -).sslip.io}
EMAIL=${2:-}

mkdir -p letsencrypt certbot-www
docker compose up -d nginx

if [ -n "$EMAIL" ]; then
  ACCOUNT="--email $EMAIL --no-eff-email"
else
  ACCOUNT="--register-unsafely-without-email"
fi

if [ ! -f "letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  # shellcheck disable=SC2086
  docker compose run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
    -d "$DOMAIN" --agree-tos --non-interactive $ACCOUNT
fi

sed "s/__DOMAIN__/$DOMAIN/g" https.conf.template > conf.d/https.conf
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload
docker compose up -d certbot

echo
echo "HTTPS activo: https://$DOMAIN/stockscreener/"
echo "URL del conector de Claude: https://$DOMAIN/stockscreener/mcp"
echo "Pon en stockscreener/.env: PUBLIC_URL=https://$DOMAIN/stockscreener"
echo "y reinicia la app: (cd ../stockscreener && docker compose up -d)"
