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

RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list

RUN apt-get update && apt-get install -y google-chrome-stable

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
COPY root/ /

RUN chmod +x /etc/cont-init.d/00-entrypoint.sh /usr/local/bin/start-init.sh && \
    cp /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html && \
    sed -i 's/rfb.scaleViewport = readQueryVariable.*$/rfb.scaleViewport = true;/' /usr/share/novnc/index.html && \
    sed -i 's/<div id="top_bar">/<div id="top_bar" style="display:none;">/' /usr/share/novnc/index.html

EXPOSE 5900
EXPOSE 80
EXPOSE 9222

RUN curl -o /tmp/hblock 'https://raw.githubusercontent.com/hectorm/hblock/v3.5.1/hblock' \
  && echo 'd010cb9e0f3c644e9df3bfb387f42f7dbbffbbd481fb50c32683bbe71f994451  /tmp/hblock' | shasum -c \
  && mv /tmp/hblock /usr/local/bin/hblock \
  && chown 0:0 /usr/local/bin/hblock \
  && chmod 755 /usr/local/bin/hblock \
  && /usr/local/bin/hblock --output /app/hosts --header none --allowlist /tmp/allowlist.txt --denylist /tmp/denylist.txt \
  && rm -f /tmp/allowlist.txt /tmp/denylist.txt

# Install browser-trace
RUN curl -fsSL "https://github.com/remotebrowser/browser-trace/releases/download/v0.3.5/browser-trace-linux-${TARGETARCH}" \
      -o /usr/local/bin/browser-trace && \
    chmod +x /usr/local/bin/browser-trace

RUN useradd -M -d /home/user -s /bin/bash user && \
    mkdir -p /home/user/chrome-profile && \
    chown -R user:user /app /home/user && \
    usermod -aG sudo user && \
    echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

RUN chmod +x /etc/s6-overlay/s6-rc.d/*/run

ENTRYPOINT ["/usr/local/bin/start-init.sh"]
