# Clipplex

Have you ever, while watching something on your plex server, wanted to easily extract a clip out of a good movie or tv show you're watching to share it with your friend, family or the world? While this was always possible, the process can be complex for something "so simple".

![](https://github.com/jo-nike/clipplex/blob/master/example.gif)

## Description

An in-depth paragraph about your project and overview of use.

## Docker variables

| Variables            | Value            | Notes     |
| ---------------------|:----------------:| ----------|
| PUID                 | 1000             | Optional  |
| GUID                 | 1000             | Optional  |
| TZ                   | America/New_York | Optional  |
| PLEX_URL             | link to plex     | Mandatory |
| PLEX_TOKEN           | token for plex   | Mandatory |
| STREAMABLE_LOGIN     | ...              | Optional  |
| STREAMABLE_PASSWORD  | ...              | Optional  |
| IMMICH_URL           | http://immich-server:2283 | Optional; Immich is enabled only with an API key |
| IMMICH_API_KEY       | ...              | Optional; see the exact permissions below |
| IMMICH_DEFAULT_TAG   | #plex-clip       | Optional; always applied to Immich uploads |
| FFMPEG_PRESET        | veryfast         | Optional x264 speed/compression tradeoff |

### Immich API key permissions

For all Clipplex Immich upload features, give the API key only these permissions:

```text
asset.upload
tag.read
tag.create
tag.asset
album.read
album.create
albumAsset.create
```

These permissions allow Clipplex to upload the video (`asset.upload`), list, create, and assign tags (`tag.read`, `tag.create`, and `tag.asset`), and list, create, and add assets to albums (`album.read`, `album.create`, and `albumAsset.create`). The `all` permission and asset read, update, delete, download, or sharing permissions are not required.

Finding Plex token: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

Volumes: You will need to mount two locations:
* one that point to your media, in the exact same fashion your plex access these media (as the path are absolutes) 
* one where the new clips will be created.

media need to be mounted into the container path: /media

clips need to be mounted into the container path: /app/app/static/media (yes, I'll get that better eventually).

Port: Port 5000 is used to serve the frontend. (yes I will serve flask with gunicorn at some point)

Network: Need to be on the same network as your plex instance.

```
docker run -d --name clipplex -p 9945:5000 -v /media:/media -v /volumes/clipplex:/app/app/static/media --restart always -e PUID=1000 -e PGID=1000 -e TZ=America/New_York -e PLEX_URL=YOURPLEXURL -e PLEX_TOKEN=YOURPLEXTOKEN jonnike/clipplex:latest
```

## Docker Compose Example
```
version: "3.5"
networks:
  docker_internal_network:
    name: plex_stack
  clipplex:
    image: jonnike/clipplex:latest
    container_name: clipplex-alpha
    networks:
      - docker_internal_network
    environment:
      - PYTHONUNBUFFERED=1
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - PLEX_URL=YOUR_PLEX_URL (example: http://plex:32400)
      - PLEX_TOKEN=YOUR_PLEX_TOKEN
      - STREAMABLE_LOGIN=YOUR_STREAMABLE_LOGIN
      - STREAMABLE_PASSWORD=YOUR_STREAMABLE_PASSWORD
      - IMMICH_URL=YOUR_IMMICH_URL (example: http://immich-server:2283)
      - IMMICH_API_KEY=YOUR_IMMICH_API_KEY
      - IMMICH_DEFAULT_TAG=#plex-clip
    volumes:
      - /media:/media
      - /volumes/clipplex:/app/app/static/media
    ports:
      - 9945:5000
```

## Authors

Contributors names and contact info

Jo Nike

## Version History

* 0.0.3
    
    Initial Release

## License

Distributed under the MIT License. See the LICENSE file information.

## Acknowledgments

* Thanks to the resident of flavourtown for allowing me to pitch my ideas and share my progress with them.

* Thanks to [Start Bootstrap](https://github.com/startbootstrap/startbootstrap-sb-admin) for the UI.
