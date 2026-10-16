# syntax=docker/dockerfile:1
# The DuoSim application: simulator + HMI + Design Studio + platform API.
# Build:  docker build -f deploy/app.Dockerfile -t duosim-app .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DUOSIM_DATA_DIR=/data
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY duosim duosim
COPY data data
# Editable install: duosim.sim locates data/snapshots relative to the package, so the source tree
# stays in place. [ai] adds the official Anthropic SDK for the Claude provider (Gemini needs nothing).
RUN pip install -e ".[ai]" && useradd --create-home --uid 10002 duosim \
 && mkdir /data && chown -R duosim /data /src/data
# fail the build, not the first request, if the calibrated nominal fileset is not where the code looks
RUN python -c "import duosim.sim as s; p = s.DATA / 'snapshots' / 'nominal.json'; assert p.exists(), p"
COPY deploy/entrypoint-app.sh /usr/local/bin/entrypoint.sh
WORKDIR /home/duosim
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/snapshots', timeout=4).status == 200 else 1)"
# root only reads the secret, then drops to uid 10002 (see entrypoint-app.sh)
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["duosim", "--host", "0.0.0.0", "--port", "8000"]
