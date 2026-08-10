# Running the unattended scanner container

Build the batch image from the repository root:

```bash
docker build -f Dockerfile.scanner -t open-seo-scanner .
```

The image runs as an unprivileged user, writes no application state inside the
image, and uses the same `open-seo-scanner` command installed by the Python
package. Mount an output directory owned by a writable host user:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD/output:/output" \
  open-seo-scanner \
  https://example.com \
  --output /output/current.json \
  --resource-csv /output/resources.csv
```

For baseline comparison and suppressions, mount the inputs read-only and keep the
new report in the output mount:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD/state:/state:ro" \
  --volume "$PWD/output:/output" \
  open-seo-scanner \
  https://example.com \
  --config /state/scanner.json \
  --baseline /state/previous.json \
  --ignore-issues /state/ignored-issue-ids.json \
  --output /output/current.json
```

The container is intentionally stateless. A scheduler should copy the completed
report to durable object storage (or another persistent state service) and mount
or download it as the next run's baseline. Exit codes are documented in the main
README; a partial scan exits `3` while still writing a valid report explaining
the coverage limit.
