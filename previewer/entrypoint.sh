#!/bin/sh
set -eu

node /opt/previewer/generate-links.mjs
exec "$@"
