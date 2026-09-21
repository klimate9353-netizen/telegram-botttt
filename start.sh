#!/bin/sh
set -eu

# Keep the unauthenticated PO Token provider private to this container.
: "${BGUTIL_POT_HOST:=127.0.0.1}"
: "${BGUTIL_POT_PORT:=4416}"

cd /opt/bgutil/server/node_modules
deno run --allow-env --allow-net --allow-ffi=. --allow-read=. \
  ../src/main.ts --host "${BGUTIL_POT_HOST}" --port "${BGUTIL_POT_PORT}" &
provider_pid=$!

# Fail clearly instead of running the bot without a PO Token provider.
sleep 1
if ! kill -0 "${provider_pid}" 2>/dev/null; then
  echo "bgutil PO Token provider failed to start" >&2
  exit 1
fi

python /app/main.py &
bot_pid=$!

shutdown() {
  kill "${bot_pid}" "${provider_pid}" 2>/dev/null || true
}
trap shutdown INT TERM

set +e
wait "${bot_pid}"
status=$?
set -e
kill "${provider_pid}" 2>/dev/null || true
wait "${provider_pid}" 2>/dev/null || true
exit "${status}"
