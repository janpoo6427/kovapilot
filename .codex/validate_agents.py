from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parent
config = tomllib.loads((ROOT / "config.toml").read_text())
assert config["agents"]["enabled"] is True
assert config["agents"]["max_concurrent_threads_per_session"] == 3

required = {"name", "description", "developer_instructions"}
names = set()
files = sorted((ROOT / "agents").glob("*.toml"))
assert len(files) == 6
for path in files:
  agent = tomllib.loads(path.read_text())
  assert required <= agent.keys()
  assert agent["name"] not in names
  names.add(agent["name"])
  print(f"{path.name}: {agent['name']}")
