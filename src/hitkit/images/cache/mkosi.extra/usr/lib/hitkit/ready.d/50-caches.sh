#!/bin/sh
# Every cache must answer through Caddy with verified TLS.
for url in https://cache.hitkit.internal:5000/v2/ https://cache.hitkit.internal:5001/v2/ \
           https://cache.hitkit.internal:5002/index/ https://cache.hitkit.internal:5003/-/ping; do
    curl -fsS --max-time 5 -o /dev/null "$url" || { echo "cache endpoint $url not answering"; exit 1; }
done
curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:3142/acng-report.html || { echo "apt-cacher-ng not answering"; exit 1; }
