FROM python:3.11-alpine3.22
RUN apk --no-cache add build-base tzdata ffmpeg font-noto-all font-noto-cjk
RUN ffmpeg -hide_banner -filters 2>&1 | grep -q " zscale " && \
    ffmpeg -hide_banner -filters 2>&1 | grep -q " tonemap "
ENV TZ=America/Chicago
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN addgroup -S -g 1000 clipplex && \
    adduser -S -D -H -u 1000 -G clipplex clipplex
COPY --chown=clipplex:clipplex . /app
RUN mkdir -p /app/app/static/media/images /app/app/static/media/videos /app/app/static/media/gifs /app/app/static/media/thumbnails && \
    chown -R clipplex:clipplex /app/app/static/media
ENV PYTHONUNBUFFERED=1
ENV FFMPEG_PRESET=veryfast
USER clipplex:clipplex
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=4", "main:app"]
