FROM golang:1.24-alpine AS builder

ARG GOPROXY=https://goproxy.cn,direct
ARG GOSUMDB=sum.golang.google.cn

ENV CGO_ENABLED=0 \
    GOOS=linux \
    GOARCH=amd64 \
    GOTELEMETRY=off \
    GOPROXY=${GOPROXY} \
    GOSUMDB=${GOSUMDB}

WORKDIR /src/gateway

RUN sed -i 's|https://dl-cdn.alpinelinux.org/alpine|https://mirrors.tuna.tsinghua.edu.cn/alpine|g' /etc/apk/repositories

COPY gateway/go.mod gateway/go.sum ./
RUN go env -w GOPROXY="${GOPROXY}" GOSUMDB="${GOSUMDB}" \
    && go mod download -x

COPY gateway/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/gateway .

FROM alpine:3.20

RUN sed -i 's|https://dl-cdn.alpinelinux.org/alpine|https://mirrors.tuna.tsinghua.edu.cn/alpine|g' /etc/apk/repositories \
    && apk add --no-cache ca-certificates wget python3 py3-pip tzdata \
    && ln -sf python3 /usr/bin/python

WORKDIR /app

ENV GATEWAY_ADDR=:8080 \
    GATEWAY_AGENT_DIR=/app/agent \
    TZ=Asia/Shanghai \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1

COPY --from=builder /out/gateway /app/gateway
COPY agent/ /app/agent/

RUN pip3 install --no-cache-dir \
    --break-system-packages \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --default-timeout=120 \
    --retries 5 \
    -r /app/agent/requirements.txt

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:8080/health" >/dev/null || exit 1

CMD ["/app/gateway"]
