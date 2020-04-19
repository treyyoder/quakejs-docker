#!/bin/sh
echo '127.0.0.1 content.quakejs.com' >> /etc/hosts

cd /var/www/html

sed -i "s/10.0.0.2/${SERVER}/g" index.html

sed -i "s/8085/${HTTP_PORT}/g" index.html

/etc/init.d/apache2 start

cd /quakejs

node build/ioq3ded.js +set fs_game baseq3 set dedicated 1 +exec server.cfg
