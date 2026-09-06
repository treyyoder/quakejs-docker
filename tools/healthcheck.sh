#!/bin/bash
# The container is healthy when the page serves, the game port listens, the
# engine has not reported a crash, and - unless it is disabled - the console
# answers. Without that last one a dead console read as healthy.
set -e
curl --fail --silent --show-error http://localhost/ > /dev/null
curl --fail --silent --show-error http://localhost/play.html > /dev/null
: < /dev/tcp/127.0.0.1/27960
! grep -q 'Server crashed:' /tmp/q3.log 2>/dev/null
if [ -n "${ADMIN_PASSWORD+set}" ] && [ -z "$ADMIN_PASSWORD" ]; then
	exit 0    # console disabled on purpose
fi
curl --fail --silent --show-error http://localhost/admin/api/ping > /dev/null
