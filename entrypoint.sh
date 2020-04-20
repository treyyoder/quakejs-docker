#!/bin/sh

cd /var/www/html

sed -i "s/quakejs/${SERVER}/g" index.html

sed -i "s/${SERVER}:80/${SERVER}:${HTTP_PORT}/g" index.html

/etc/init.d/apache2 start

cd /quakejs

node build/ioq3ded.js +set fs_game baseq3 set dedicated 1 +exec server.cfg
