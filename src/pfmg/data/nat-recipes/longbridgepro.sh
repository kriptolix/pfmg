#!/bin/bash

export FONTCONFIG_FILE=/app/share/fonts/fonts.conf
exec /app/extra/longbridge "$@"
