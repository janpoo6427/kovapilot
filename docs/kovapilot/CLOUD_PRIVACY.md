# Kovapilot cloud privacy policy

Kovapilot records local drive logs for debugging and safety validation, but
comma cloud connectivity and route uploads are disabled by default.

The build-time policy lives in
`openpilot/kovapilot/config/cloud_privacy.py`:

```python
COMMA_CONNECT_ENABLED = False
COMMA_ROUTE_UPLOAD_ENABLED = False
```

These values are not Params and are not exposed in the UI. Changing them
requires a reviewed source change and a new deployment.

## Disabled paths

- `manage_athenad` is not started while comma connect is disabled.
- Direct `manage_athenad` and `athenad` entry points return before reading
  device identity or opening cloud connections while comma connect is disabled.
- `uploader` is not started while route uploads are disabled.
- `uploader.main()`, `Uploader.upload()`, and `Uploader.do_upload()` refuse to
  upload while the route-upload policy is disabled.
- Athena upload RPCs reject new requests, its upload workers stay idle, and
  direct Athena upload calls are rejected while uploads are disabled.

## Preserved local behavior

- `loggerd` and `encoderd` continue to run onroad.
- Local qlog, rlog, and camera files remain available for private debugging,
  replay, and vehicle integration evidence.
- Software downloads, local SSH, and APIs unrelated to route upload are not
  globally blocked by this policy.

Pending Athena queue metadata and local route files are not deleted. With the
default policy they remain inert and are not transmitted.
