#!/bin/sh
set -eu

bucket="${MINIO_BUCKET:-limra-artifacts}"

mc alias set limra http://minio:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"
mc mb --ignore-existing "limra/${bucket}"
mc anonymous set none "limra/${bucket}"
