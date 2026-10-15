FROM docker:cli

# docker CLI + compose plugin (base), git for the pull, python3 for the server
RUN apk add --no-cache git python3 docker-cli-compose

WORKDIR /app
COPY update.sh server.py ./
RUN chmod +x update.sh

EXPOSE 8002
CMD ["python3", "server.py"]
