FROM python:3.14-slim

WORKDIR /app

# gcc para compilar TgCrypto
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium de Playwright para el captcha de filecrypt. Opcional: añade ~700 MB
# y en un servidor sin pantalla el captcha no puede resolverse de todas formas.
#   docker build --build-arg INSTALL_BROWSER=true -t debrid-bot .
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then playwright install --with-deps chromium; fi

# yt-dlp opcional (YouTube/Vimeo/…). Activa YTDLP=true en el .env.
# ffmpeg permite fusionar vídeo+audio. Ejemplo:
#   docker build --build-arg INSTALL_YTDLP=true -t debrid-bot .
ARG INSTALL_YTDLP=false
RUN if [ "$INSTALL_YTDLP" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends ffmpeg \
      && rm -rf /var/lib/apt/lists/* \
      && pip install --no-cache-dir yt-dlp; \
    fi

COPY . .

# puerto del relay de enlaces (LINK_PROXY)
EXPOSE 8845

VOLUME ["/app/downloads"]

CMD ["python", "main.py"]
