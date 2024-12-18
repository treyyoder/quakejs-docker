# Stage 1: Base setup
FROM ubuntu:20.04 AS base

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Europe/London

# Update system and install required packages
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    sudo \
    curl \
    git \
    nodejs \
    npm \
    jq \
    apache2 \
    wget \
    apt-utils && \
    rm -rf /var/lib/apt/lists/*

# Install Node.js 12.x
RUN curl -sL https://deb.nodesource.com/setup_12.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Clone QuakeJS repository and install npm dependencies
RUN git clone https://github.com/nerosketch/quakejs.git /quakejs
WORKDIR /quakejs
RUN npm install

# Stage 2: Copy static files
FROM base AS builder

# Copy assets and configuration files
COPY ./include/ioq3ded/ioq3ded.fixed.js /quakejs/build/ioq3ded.js
COPY ./include/assets/ /var/www/html/assets
RUN rm /var/www/html/index.html && \
    cp /quakejs/html/* /var/www/html/

# Stage 3: Final stage (lightweight runtime)
FROM ubuntu:20.04

# Set non-interactive frontend and timezone
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Europe/London

# Install only runtime dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    apache2 \
    nodejs \
    npm && \
    rm -rf /var/lib/apt/lists/*

# Copy necessary files from the builder stage
COPY --from=builder /quakejs /quakejs
COPY --from=builder /var/www/html /var/www/html

# Copy and set up server configuration files
COPY server.cfg /quakejs/base/baseq3/server.cfg
COPY server.cfg /quakejs/base/cpma/server.cfg

# Add entrypoint script and ensure it is executable
ADD entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
