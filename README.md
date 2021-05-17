<div align="center">
    
![logo](https://github.com/treyyoder/quakejs-docker/blob/master/quakejs-docker.png?raw=true)
# quakejs-docker 

![Docker Image CI](https://github.com/treyyoder/quakejs-docker/workflows/Docker%20Image%20CI/badge.svg)
</div>
:warning: 4/22/2020 Fixed a bug that was preventing other maps from loading.  Pull the lastest image from Docker Hub.

:warning: 4/20/2020 Issues with entrypoint permissions and the refresh loop have been addressed.  Pull the lastest image from Docker Hub.

### A fully local and Dockerized quakejs server. Independent, unadulterated, and free from the middleman.  

The goal of this project was to create a fully independent quakejs server in Docker that does not require content to be served from the internet.
Hence, once pulled, this does not need to connect to any external provider, ie. content.quakejs.com. Nor does this server need to be proxied/served/relayed from quakejs.com

#### Simply pull the image [treyyoder/quakejs](https://hub.docker.com/r/treyyoder/quakejs)
```
docker pull treyyoder/quakejs:latest
```
#### and run it:

```
docker run -d --name quakejs -e HTTP_PORT=<HTTP_PORT> -p <HTTP_PORT>:80 -p 27960:27960 treyyoder/quakejs:latest
```

#### Example:

```
docker run -d --name quakejs -e HTTP_PORT=8080 -p 8080:80 -p 27960:27960 treyyoder/quakejs:latest
```

Send all you friends/coworkers the link: ex. http://localhost:8080 and start fragging ;)

#### server.cfg:
Refer to [quake3world](https://www.quake3world.com/q3guide/servers.html) for instructions on its usage.

#### docker-compose.yml
```
version: '2'
services:
    quakejs:
        container_name: quakejs
        environment:
            - HTTP_PORT=8080
        ports:
            - '8080:80'
            - '27960:27960'
        image: 'treyyoder/quakejs:latest'
```


## Credits:

Thanks to [begleysm](https://github.com/begleysm) with his [fork](https://github.com/begleysm/quakejs) of [quakejs](https://github.com/inolen/quakejs) to which this was derived, aswell as his thorough [documentation](https://steamforge.net/wiki/index.php/How_to_setup_a_local_QuakeJS_server_under_Debian_9_or_Debian_10)
