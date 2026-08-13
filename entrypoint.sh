#!/bin/sh
set -eu

# Rewrite the client args so they adapt to whatever origin the browser used:
# assets come from the page's own host:port, and the game connects over the same
# port (Apache proxies websocket upgrades to the internal game server on 27960).
cd /var/www/html
sed -i "s/'quakejs:80'/window.location.host/g" index.html
sed -i "s/'quakejs:27960'/window.location.hostname + ':' + (window.location.port || (window.location.protocol === 'https:' ? '443' : '80'))/g" index.html

/etc/init.d/apache2 start

cd /quakejs
exec node build/ioq3ded.js +set fs_cdn localhost:80 +set fs_game baseq3 +set dedicated 1 +exec server.cfg
