# syntax=docker/dockerfile:1
# A FreeCAD worker: real FreeCAD GUI on a virtual display + the FreeCAD-MCP addon RPC server.
# Build:  docker build -f deploy/freecad-worker.Dockerfile -t duosim-freecad .
# One container = one FreeCAD session (the addon serialises work on FreeCAD's GUI thread);
# scale by running more containers behind the app's worker setting.

FROM ubuntu:24.04 AS fetch
ARG FREECAD_VERSION=1.1.4
# from the release's FreeCAD_<ver>-Linux-x86_64-py311.AppImage-SHA256.txt - the build fails if it differs
ARG FREECAD_SHA256=f6dc6ba676e5ac96a565ebc8d657232f94c6158e85b4352141bd1a46f6b43434
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN curl -fL --retry 3 -o fc.AppImage \
      "https://github.com/FreeCAD/FreeCAD/releases/download/${FREECAD_VERSION}/FreeCAD_${FREECAD_VERSION}-Linux-x86_64-py311.AppImage" \
 && echo "${FREECAD_SHA256}  fc.AppImage" | sha256sum -c - \
 && chmod +x fc.AppImage && ./fc.AppImage --appimage-extract >/dev/null \
 && rm fc.AppImage

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 xvfb xauth libgl1-mesa-dri libglx-mesa0 libegl1 libxkbcommon-x11-0 libxcb-cursor0 \
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
      libxcb-xinerama0 libxcb-xkb1 libxcb-xfixes0 libdbus-1-3 libfontconfig1 fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 freecad
COPY --from=fetch /dl/squashfs-root /opt/freecad
WORKDIR /app
# only what the worker needs: the launcher, the RPC client it imports, and the vendored addon
COPY duosim/__init__.py duosim/freecad_bridge.py duosim/freecad_worker.py duosim/
COPY third_party/freecad-mcp third_party/freecad-mcp
# the healthcheck reads the token copy the entrypoint makes (the RPC refuses unauthenticated calls)
ENV DUOSIM_FREECAD=/opt/freecad PYTHONUNBUFFERED=1 FREECAD_MCP_TOKEN_FILE=/run/freecad/token
COPY deploy/entrypoint-freecad.sh /app/entrypoint.sh
EXPOSE 9875
# private ranges by default (loopback is always added for the healthcheck); narrow to your app network in production
ENV ALLOWED_IPS="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
HEALTHCHECK --interval=15s --timeout=8s --start-period=60s --retries=4 \
  CMD python3 -c "import sys; from duosim.freecad_bridge import FreeCADClient; d = FreeCADClient('127.0.0.1', 9875).diagnose(); sys.exit(0 if d['ok'] else 1)"
# starts as root only to read the secret, then drops to uid 10001 (see entrypoint-freecad.sh)
ENTRYPOINT ["/app/entrypoint.sh"]
