"""Build-time cloud privacy policy for kovapilot.

These constants intentionally cannot be changed through Params, the UI, or a
remote service. Enabling comma cloud connectivity or route uploads requires a
reviewed source change and a new deployment.
"""

COMMA_CONNECT_ENABLED = False
COMMA_ROUTE_UPLOAD_ENABLED = False
