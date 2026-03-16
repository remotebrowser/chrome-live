FROM mirror.gcr.io/library/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscf-eula select true" | debconf-set-selections

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    software-properties-common && \
    add-apt-repository multiverse && \
    apt-get update -y && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
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
    gtk2-engines-murrine \
    dbus-x11 \
    novnc \
    websockify \
    x11-apps \
    sudo \
    socat \
    screen \
    sqlite3 \
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


WORKDIR /app

COPY entrypoint.sh /app/entrypoint.sh
COPY tinyproxy.conf /app/tinyproxy.conf
COPY allowlist.txt /app/allowlist.txt

EXPOSE 5900

RUN cp /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html
RUN sed -i 's/rfb.scaleViewport = readQueryVariable.*$/rfb.scaleViewport = true;/' /usr/share/novnc/index.html
RUN sed -i 's/<div id="top_bar">/<div id="top_bar" style="display:none;">/' /usr/share/novnc/index.html
EXPOSE 80
EXPOSE 9222

RUN curl -o /tmp/hblock 'https://raw.githubusercontent.com/hectorm/hblock/v3.5.1/hblock' \
  && echo 'd010cb9e0f3c644e9df3bfb387f42f7dbbffbbd481fb50c32683bbe71f994451  /tmp/hblock' | shasum -c \
  && sudo mv /tmp/hblock /usr/local/bin/hblock \
  && sudo chown 0:0 /usr/local/bin/hblock \
  && sudo chmod 755 /usr/local/bin/hblock \
  && /usr/local/bin/hblock --output /app/hosts --header none --allowlist /app/allowlist.txt

RUN useradd -m -s /bin/bash user && \
    chown -R user:user /app && \
    usermod -aG sudo user && \
    echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER user

RUN mkdir -p $HOME/chrome-profile

ENTRYPOINT ["/app/entrypoint.sh"]
