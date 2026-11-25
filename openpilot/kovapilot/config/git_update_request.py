import re

KEY = "KovaGitUpdateRequest"
ACTIVE_PHASES = ("queued", "staged")
PLAN_FIELDS = ("operation", "branch", "commit", "branchHead", "beforeHead", "beforeBranch")


def active_request(params):
  request = params.get(KEY)
  if not isinstance(request, dict) or request.get("phase") not in ACTIVE_PHASES:
    return None
  if request.get("operation") not in ("switch", "reset"):
    return None
  branch = request.get("branch")
  if not isinstance(branch, str) or not branch or branch.startswith("-"):
    return None
  if any(not isinstance(request.get(field), str) or not re.fullmatch(r"[0-9a-f]{40}", request[field])
         for field in ("commit", "branchHead", "beforeHead")):
    return None
  return request


def describe_request(request):
  if isinstance(request, dict) and request.get("phase") in (*ACTIVE_PHASES, "installed", "failed"):
    return {key: request[key] for key in ("phase", "branch", "commit", "reason") if key in request}
  return None
