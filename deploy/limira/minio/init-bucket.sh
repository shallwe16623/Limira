#!/bin/sh
set -eu

bucket="${MINIO_BUCKET:-limira-artifacts}"

mc alias set limira http://minio:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"
mc mb --ignore-existing "limira/${bucket}"
mc anonymous set none "limira/${bucket}"
