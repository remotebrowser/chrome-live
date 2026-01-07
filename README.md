# Chrome Live

<img width="800" src="screenshot.jpg" alt="Screenshot" />

Run a containerized Google Chrome on Linux, accessible from any web browser.

Try using Docker:
```
docker run --name chrome-live -p 7000:80 ghcr.io/remotebrowser/chrome-live
```
or Podman:
```
podman run --name chrome-live -p 7000:80 ghcr.io/remotebrowser/chrome-live
```
Then open `localhost:7000` in your browser.

To enable remote control of Chrome via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/), map port 9222 as well:
```
podman run --name chrome-live -p 7000:80 -p 9222:9222 ghcr.io/remotebrowser/chrome-live
```

To configure Chrome's proxy connection (via [GOST](https://gost.run/en)):
```
podman exec chrome-live bash -c "sudo pkill gost"
podman exec chrome-live bash -c "screen -dmS gost /app/gost -L http://:8080 -F http://username:password@proxy"
```

To test the CDP connection:
```
curl http://127.0.0.1:9222/json/list
```

To build and run locally:
```
docker build -t chrome-live .
docker run -p 7000:80 chrome-live
```