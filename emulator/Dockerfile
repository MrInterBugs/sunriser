FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends libslirp0 \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /emulator
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sunriser_emu sunriser_emu

VOLUME /state
EXPOSE 8080
ENTRYPOINT ["python", "-m", "sunriser_emu", "--listen", "0.0.0.0"]
