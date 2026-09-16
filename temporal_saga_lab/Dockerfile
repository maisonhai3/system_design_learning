# One image, five binaries. Each service in docker-compose.yml runs a different
# `command` out of it.
#
# That is a lab convenience, not a recommendation: sharing an image means every
# service redeploys when any of them changes, which is the opposite of what
# separate services are for. It is here because the alternative — five
# Dockerfiles that differ by one line — would teach nothing and break more often.
FROM golang:1.25-alpine AS build
WORKDIR /src

# Dependencies first, so editing a scenario does not re-download the SDK.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# CGO off: modernc.org/sqlite is pure Go, so the binaries are static and the
# runtime image needs nothing but certificates.
RUN CGO_ENABLED=0 go build -trimpath -o /out/ ./cmd/...

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=build /out/ /usr/local/bin/
# busybox wget is the healthcheck client; there is no curl in here.
WORKDIR /data
