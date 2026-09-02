# syntax=docker/dockerfile:1
# Build stage: compile browser-trace into a standalone binary with PyInstaller.
FROM python:3.12-slim AS browser-trace-builder

# PyInstaller shells out to objdump (part of binutils) when analysing binaries
# on Linux; python:3.12-slim doesn't ship it, so install just that.
RUN apt-get update -y && apt-get install -y --no-install-recommends binutils && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /src
COPY browser-trace/pyproject.toml browser-trace/uv.lock browser-trace/.python-version ./
COPY browser-trace/main.py browser-trace/cdp.py browser-trace/logs.py browser-trace/thumbnail.py browser-trace/recording.py browser-trace/server.py browser-trace/traffic.py browser-trace/upload.py browser-trace/captcha_classifier.js browser-trace/browser-trace.spec ./

# Install the locked runtime deps + the dev group (pyinstaller), then build the
# onefile binary. `--frozen` keeps the build reproducible against uv.lock. Build
# from the spec (not `--onefile main.py`) so captcha_classifier.js is bundled as
# a data file — main.py reads it at runtime from sys._MEIPASS when frozen.
RUN uv sync --frozen --group dev
RUN uv run --group dev pyinstaller browser-trace.spec


FROM mirror.gcr.io/library/ubuntu:24.04

ARG TARGETARCH
ARG S6_OVERLAY_VERSION=v3.2.2.0

ENV DEBIAN_FRONTEND=noninteractive

RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscf-eula select true" | debconf-set-selections

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    software-properties-common && \
    add-apt-repository multiverse && \
    apt-get update -y && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    ffmpeg \
    tinyproxy \
    xterm \
    tigervnc-standalone-server \
    xfonts-base \
    xfonts-75dpi \
    xfonts-100dpi \
    xfce4 \
    xfce4-goodies \
    xfconf \
    tar \
    tzdata \
    xz-utils \
    gtk2-engines-murrine \
    dbus-x11 \
    novnc \
    websockify \
    x11-apps \
    sudo \
    socat \
    screen \
    sqlite3 \
    procps \
    cabextract \
    fontconfig \
    ttf-mscorefonts-installer \
    fonts-freefont-ttf \
    fonts-gfs-neohellenic \
    fonts-indic \
    fonts-ipafont-gothic \
    fonts-kacst \
    fonts-liberation \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-roboto \
    fonts-thai-tlwg \
    fonts-ubuntu \
    fonts-wqy-zenhei \
    fonts-open-sans \
    && fc-cache -f -v

# Default the whole container to PST instead of the implicit UTC
ENV TZ=America/Los_Angeles
RUN ln -sf /usr/share/zoneinfo/America/Los_Angeles /etc/localtime && \
    echo "America/Los_Angeles" > /etc/timezone

RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list

RUN apt-get update && apt-get install -y google-chrome-stable

# Install CloakBrowser alongside Google Chrome. Both browsers live in the same
# image; the chromium s6 service picks which one to launch at runtime (default:
# google-chrome-stable). CloakBrowser publishes no arm64 binary, so it stays
# amd64-only; the TARGETARCH guard below keeps that step a no-op on arm64.
RUN if [ "${TARGETARCH}" = "amd64" ]; then \
      apt-get update -y && apt-get install -y --no-install-recommends \
        python3-pip libgl1 libgl1-mesa-dri && \
      pip3 install --break-system-packages cloakbrowser && \
      python3 -m cloakbrowser install && \
      CLOAK_DIR=$(find /root -name chrome -path '*cloakbrowser*' -type f 2>/dev/null | head -1 | xargs dirname) && \
      echo "CloakBrowser dir: ${CLOAK_DIR}" && \
      cp -r "${CLOAK_DIR}" /usr/local/lib/cloakbrowser && \
      chmod 755 /usr/local/lib/cloakbrowser/chrome && \
      ln -sf /usr/local/lib/cloakbrowser/chrome /usr/local/bin/cloak-browser && \
      rm -rf /var/lib/apt/lists/* ; \
    else \
      echo "Skipping CloakBrowser install on ${TARGETARCH} (amd64 only)" ; \
    fi

# Install custom-chromium alongside Chrome/CloakBrowser. Source-patched Chromium
# (see its RUNNING.md); the amd64 build, same arch as Chrome/CloakBrowser and as
# the Daytona host, so no QEMU/multi-platform build needed. Bind-mount the
# context instead of COPY so the file only needs to be present when it's
# actually used. Required on amd64: fail loud rather than silently ship an
# image missing custom-chrome.
ARG CUSTOM_CHROMIUM_TARBALL=custom-chromium-151.0.7922.71-release-linux-x64.tar.zst
RUN --mount=type=bind,source=.,target=/ctx \
    if [ "${TARGETARCH}" = "amd64" ] && [ -f "/ctx/${CUSTOM_CHROMIUM_TARBALL}" ]; then \
      apt-get update -y && apt-get install -y --no-install-recommends zstd && \
      mkdir -p /usr/local/lib/custom-chromium && \
      tar --zstd -xf "/ctx/${CUSTOM_CHROMIUM_TARBALL}" --strip-components=1 -C /usr/local/lib/custom-chromium && \
      chmod 755 /usr/local/lib/custom-chromium/chrome /usr/local/lib/custom-chromium/chrome-wrapper && \
      ln -sf /usr/local/lib/custom-chromium/chrome-wrapper /usr/local/bin/custom-chrome && \
      rm -rf /var/lib/apt/lists/* ; \
    elif [ "${TARGETARCH}" = "amd64" ]; then \
      echo "FATAL: ${CUSTOM_CHROMIUM_TARBALL} not found in build context (amd64 build requires it)" >&2 && \
      exit 1 ; \
    else \
      echo "Skipping custom-chromium install on ${TARGETARCH} (amd64 only)" ; \
    fi

# Install s6-overlay
RUN case "${TARGETARCH}" in \
      amd64) S6_ARCH="x86_64" ;; \
      arm64) S6_ARCH="aarch64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" -o /tmp/s6-overlay-noarch.tar.xz && \
    curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" -o /tmp/s6-overlay-arch.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz && \
    rm -f /tmp/s6-overlay-noarch.tar.xz /tmp/s6-overlay-arch.tar.xz

WORKDIR /app

COPY entrypoint.sh /etc/cont-init.d/00-entrypoint.sh
COPY start-init.sh /usr/local/bin/start-init.sh
COPY tinyproxy.conf /app/tinyproxy.conf
COPY browser-trace.conf /app/browser-trace.conf
COPY allowlist.txt /tmp/allowlist.txt
COPY denylist.txt /tmp/denylist.txt
COPY hosts-to-filter.awk /tmp/hosts-to-filter.awk
COPY root/ /

RUN chmod +x /etc/cont-init.d/00-entrypoint.sh /usr/local/bin/start-init.sh && \
    cp /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html && \
    sed -i 's/rfb.scaleViewport = readQueryVariable.*$/rfb.scaleViewport = true;/' /usr/share/novnc/index.html && \
    sed -i 's/<div id="top_bar">/<div id="top_bar" style="display:none;">/' /usr/share/novnc/index.html

EXPOSE 5900
EXPOSE 80
EXPOSE 8080
EXPOSE 9222
EXPOSE 8088

RUN curl -o /tmp/hblock 'https://raw.githubusercontent.com/hectorm/hblock/v3.5.1/hblock' \
  && echo 'd010cb9e0f3c644e9df3bfb387f42f7dbbffbbd481fb50c32683bbe71f994451  /tmp/hblock' | shasum -c \
  && mv /tmp/hblock /usr/local/bin/hblock \
  && chown 0:0 /usr/local/bin/hblock \
  && chmod 755 /usr/local/bin/hblock \
  && /usr/local/bin/hblock --output /app/hosts --header none --allowlist /tmp/allowlist.txt --denylist /tmp/denylist.txt \
  && awk -f /tmp/hosts-to-filter.awk /app/hosts > /app/tinyproxy-filter.txt \
  && test "$(wc -l < /app/tinyproxy-filter.txt)" -gt 500000 \
  && rm -f /tmp/allowlist.txt /tmp/denylist.txt /tmp/hosts-to-filter.awk

COPY --from=browser-trace-builder /src/dist/browser-trace /usr/local/bin/browser-trace
RUN chmod +x /usr/local/bin/browser-trace

COPY switch-browser.sh /usr/local/bin/switch-browser
RUN chmod +x /usr/local/bin/switch-browser

RUN useradd -M -d /home/user -s /bin/bash user && \
    mkdir -p /home/user/chrome-profile && \
    chown -R user:user /app /home/user && \
    usermod -aG sudo user && \
    echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

RUN chmod +x /etc/s6-overlay/s6-rc.d/*/run

ENTRYPOINT ["/usr/local/bin/start-init.sh"]
