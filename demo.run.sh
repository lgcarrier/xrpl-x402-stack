#!/bin/bash

ASSET_ENV=$1

pick_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

FACILITATOR_PORT=$(pick_port)
MERCHANT_PORT=$(pick_port)
while [ "$MERCHANT_PORT" = "$FACILITATOR_PORT" ]; do
  MERCHANT_PORT=$(pick_port)
done

PROJECT_NAME="demo_$(basename $ASSET_ENV | tr '.' '_' )_$$"

FACILITATOR_PORT=$FACILITATOR_PORT MERCHANT_PORT=$MERCHANT_PORT docker compose \
  --project-name "$PROJECT_NAME" \
  --env-file "$ASSET_ENV" \
  --profile demo \
  run --rm buyer
