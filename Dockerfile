# Use current Ubuntu LTS as the base image
FROM ubuntu:latest

# Update apt-get and install essential tools like curl, gpg, git, nginx, and supervisor
RUN apt-get update && \
    apt-get install -y curl gpg git nginx supervisor

# Set Node.js major version for installation
ENV NODE_MAJOR=20

# Add the NodeSource GPG key and repository for Node.js
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y nodejs

# Set the working directory for the QuakeJS server
WORKDIR /quakejs

# Clone the QuakeJS server repository and install dependencies
RUN git clone https://github.com/begleysm/quakejs . && \
    npm install

# Copy server configuration files
COPY server.cfg /quakejs/base/baseq3/
COPY server.cfg /quakejs/base/cpma/

# Replace the fixed JavaScript file for ioq3ded
COPY ./include/ioq3ded/ioq3ded.fixed.js /quakejs/build/ioq3ded.js

# Modify the QuakeJS HTML to dynamically set the hostname and protocol for resources
RUN sed -i "s#'quakejs:[0-9]\+'#window.location.hostname#g" /quakejs/html/index.html && \
    sed -i "s#var url = 'http://' + fs_cdn + '/assets/manifest.json';#var url = '//' + window.location.host + '/assets/manifest.json';#" /quakejs/html/ioquake3.js && \
    sed -i "s#var url = 'http://' + root + '/assets/' + name;#var url = '//' + window.location.host + '/assets/' + name;#" /quakejs/html/ioquake3.js && \
    sed -i "s#var url = 'ws://' + addr + ':' + port;#var url = window.location.protocol.replace('http', 'ws') + window.location.host;#" /quakejs/html/ioquake3.js

# Link QuakeJS to the nginx web root
RUN rm -rf /var/www/html && ln -s /quakejs/html /var/www/html

# Copy game assets to the web root
COPY ./include/assets/ /var/www/html/assets

# Remove unnecessary packages to reduce image size
RUN apt-get purge curl gpg git -y && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Configure supervisord and nginx
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY nginx.conf /etc/nginx/sites-available/default

# Create a non-root user for running the QuakeJS server
RUN groupadd -r quakejs && useradd -r -g quakejs quakejs

# Set permissions for the QuakeJS server and web root
RUN chown -R quakejs:quakejs /quakejs /var/www/html

# Start the supervisor daemon
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]