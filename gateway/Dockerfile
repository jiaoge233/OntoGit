FROM golang:1.24-alpine AS builder

ENV CGO_ENABLED=0 \
    GOOS=linux \
    GOARCH=amd64 \
    GOTELEMETRY=off

WORKDIR /src

COPY go.mod ./
RUN go mod download

COPY . .
RUN go build -trimpath -ldflags="-s -w" -o /out/gateway .

FROM alpine:3.20

RUN apk add --no-cache ca-certificates wget

WORKDIR /app

ENV GATEWAY_ADDR=:8080

COPY --from=builder /out/gateway /app/gateway

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:8080/health" >/dev/null || exit 1

CMD ["/app/gateway"]
