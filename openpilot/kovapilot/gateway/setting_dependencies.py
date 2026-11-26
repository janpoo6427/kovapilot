from typing import Any


def evaluate_dependencies(settings: list[dict[str, Any]], conditions: dict[str, str]) -> None:
  values = {setting["key"]: setting["value"] for setting in settings}
  for setting in settings:
    for dependency in setting.get("dependencies", ()):
      if "condition" in dependency:
        dependency["state"] = conditions.get(dependency["condition"], "unknown")
      else:
        key = dependency["setting"]
        dependency["state"] = ("unknown" if key not in values else
                               "met" if values[key] == dependency["equals"] else "unmet")
