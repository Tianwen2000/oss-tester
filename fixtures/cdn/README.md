# CDN Origin Fixtures

These deterministic files are generated and uploaded by:

```bash
python3 oss_cli.py seed-cdn-fixtures --confirm-bucket
```

The uploader adds a unique run prefix and writes
`reports/cdn-fixtures-<run_id>.json`. The `redirect/` and `errors/` files are
origin payloads; CDN routing rules must produce the corresponding HTTP status
codes listed in the manifest.
