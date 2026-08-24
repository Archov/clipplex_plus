FROM python:3.11-alpine3.22
ARG PLEX_URL
ARG PLEX_TOKEN
ARG STREAMABLE_LOGIN
ARG STREAMABLE_PASSWORD
RUN apk --no-cache add build-base tzdata ffmpeg font-noto-all font-noto-cjk
RUN ffmpeg -hide_banner -filters 2>&1 | grep -q " zscale " && \
    ffmpeg -hide_banner -filters 2>&1 | grep -q " tonemap "
ENV TZ=America/New_York
ENV PLEX_URL=$PLEX_URL
ENV PLEX_TOKEN=$PLEX_TOKEN
ENV STREAMABLE_LOGIN=$STREAMABLE_LOGIN
ENV STREAMABLE_PASSWORD=$STREAMABLE_PASSWORD
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . /app
ENV FLASK_APP=main.py
ENV PYTHONUNBUFFERED=1
ENV FFMPEG_PRESET=veryfast
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=5000"]
