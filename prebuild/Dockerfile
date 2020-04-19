FROM ubuntu:latest

RUN apt update
RUN apt upgrade -y

RUN apt-get install sudo curl git nodejs npm -y

RUN curl -sL https://deb.nodesource.com/setup_12.x | sudo -E bash -

#RUN adduser quake
#RUN usermod -aG sudo quake
#USER quake

RUN git clone --recurse-submodules https://github.com/begleysm/quakejs.git
WORKDIR /quakejs

RUN npm install
RUN node build/ioq3ded.js +set fs_game baseq3 +set dedicated 1
