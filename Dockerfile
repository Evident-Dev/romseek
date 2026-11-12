# RomSeek runtime: stdlib Python + 7-Zip and unrar (extract rar/7z/split archives on import).
# Debian base: Alpine has no unrar and its 7zip is built without the RAR codec.
FROM python:3.12-slim-bookworm
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends 7zip unrar \
 && rm -rf /var/lib/apt/lists/*
USER 1000:1000
CMD ["python3", "-u", "/app/romseek.py"]
