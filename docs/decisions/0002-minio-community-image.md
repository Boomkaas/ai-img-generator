# 0002. Use the pgsty community build of the MinIO image

- Status: accepted
- Date: 2026-10-15

## Context

The architecture (§2, §5.2) and step 01 name the `minio/minio` and `minio/mc` images. Upstream MinIO no
longer publishes them: `docker pull minio/minio` fails with "repository does not exist", and
`quay.io/minio/minio` answers "unauthorized". The service itself (S3-compatible storage accessed through
boto3) is unchanged.

## Decision

Use `pgsty/minio` and `pgsty/mc`, the Pigsty project's maintained builds of the same MinIO server and
client, published on Docker Hub. They take the same command line, environment variables and ports, and
the server image includes `curl` and `mc` for health checks. `infra/compose.dev.yml` and the stack file
in `docs/architecture.md` §5.2 use them.

## Consequences

- No code changes: boto3 talks to the same S3 API.
- We depend on a third-party build. If it stops being maintained, `cgr.dev/chainguard/minio` is a drop-in
  alternative, or the images can be built from MinIO's source in `infra/`.
- Production images should pin a release tag rather than `latest` (step 12).
