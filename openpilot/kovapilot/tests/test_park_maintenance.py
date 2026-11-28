"""KovaLink changes in Park with the vehicle on, and a comma restart at any time (janpoo6427 2026-11-27)."""

import subprocess
from types import SimpleNamespace

import pytest

from openpilot.kovapilot.config.git_update_request import KEY as GIT_UPDATE_KEY
from openpilot.kovapilot.config.kova_params import KovaParamsApplyPolicyError, KovaParamsStore, load_bundled_catalog
from openpilot.kovapilot.config.parked_setting import parked_and_disengaged
from openpilot.kovapilot.gateway.device_restart import DeviceRestart, restart_allowed
from openpilot.kovapilot.gateway.git_maintenance import GitRepository
from openpilot.kovapilot.tests.test_kova_params import MemoryBackend
from openpilot.system.manager import process_config
from openpilot.system.updated import updated


NOW = 1_010_000_000
SERVICES = ("carState", "carControl", "selfdriveState")


class Sample(dict):
  def __init__(self, **values):
    super().__init__(**values)
    self.valid = dict.fromkeys(SERVICES, True)
    self.alive = dict.fromkeys(SERVICES, True)
    self.logMonoTime = dict.fromkeys(SERVICES, 1_000_000_000)
    self.updates = 0

  def update(self, timeout):
    self.updates += 1


def parked_sample():
  return Sample(carState=SimpleNamespace(vEgo=0., canValid=True, standstill=True, gearShifter="park"),
                carControl=SimpleNamespace(enabled=False, latActive=False, longActive=False),
                selfdriveState=SimpleNamespace(enabled=False, active=False))


def test_parked_and_disengaged_needs_park_standstill_and_released_controls():
  sm = parked_sample()
  assert parked_and_disengaged(sm, NOW)
  for service, field, value in (("carState", "gearShifter", "drive"), ("carState", "standstill", False),
                                ("carState", "vEgo", 0.5), ("carState", "canValid", False),
                                ("carControl", "enabled", True), ("carControl", "latActive", True),
                                ("carControl", "longActive", True), ("selfdriveState", "enabled", True),
                                ("selfdriveState", "active", True)):
    sm = parked_sample()
    setattr(sm[service], field, value)
    assert not parked_and_disengaged(sm, NOW), (service, field)
  for service in SERVICES:
    sm = parked_sample()
    sm.logMonoTime[service] = 0
    assert not parked_and_disengaged(sm, NOW), service
  assert not parked_and_disengaged(parked_sample(), 1_600_000_000)


def test_restart_with_the_device_rule_needs_only_authorization():
  calls = []
  restart = DeviceRestart(restart_allowed, lambda: calls.append(1))
  assert restart.enabled(True)
  assert not restart.enabled(False)
  restart.restart(True)
  assert calls == [1]


def test_offroad_policy_setting_is_saved_in_park_but_not_while_driving():
  catalog = load_bundled_catalog()
  sample = next(s for s in catalog["settings"] if s["key"] == "steering.always_on")
  sample.pop("savePolicy")
  sample["applyPolicy"] = "offroad"
  backend = MemoryBackend()
  store = KovaParamsStore(backend, catalog)
  with pytest.raises(KovaParamsApplyPolicyError):
    store.update("steering.always_on", 1, 0, is_offroad=False, is_parked=False)
  assert backend.put_calls == []
  assert store.update("steering.always_on", 1, 0, is_offroad=False, is_parked=True)["revision"] == 1


def test_git_status_names_park_as_the_blocking_condition(tmp_path):
  subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=tmp_path)
  subprocess.check_call(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "--allow-empty", "-qm", "fixture"], cwd=tmp_path)
  state = {"allowed": False}
  repo = GitRepository(tmp_path, apply_allowed=lambda: state["allowed"], enqueue=lambda job: None)
  status = repo.status(True)
  assert status["mutationEnabled"] is False
  assert status["mutationBlock"] == "parkedOrUpdaterBusy"
  assert repo.status(False)["mutationBlock"] == "authorization"
  state["allowed"] = True
  assert repo.status(True)["mutationBlock"] is None


class FakeParams:
  def __init__(self, **values):
    self.data = values

  def get(self, key):
    return self.data.get(key)

  def get_bool(self, key):
    return bool(self.data.get(key))

  def put(self, key, value, *, block):
    self.data[key] = value

  put_bool = put


def updater_with(params, sm):
  updater = object.__new__(updated.Updater)
  updater.params = params
  updater._parked_sm = sm
  return updater


def test_updater_maintenance_is_allowed_offroad_or_parked_and_disengaged(monkeypatch):
  assert updater_with(FakeParams(IsOffroad=True), None).link_maintenance_allowed()
  monkeypatch.setattr(updated.time, "monotonic_ns", lambda: NOW)
  sm = parked_sample()
  assert updater_with(FakeParams(IsOffroad=False), sm).link_maintenance_allowed()
  assert sm.updates == 1
  sm = parked_sample()
  sm["carState"].gearShifter = "drive"
  assert not updater_with(FakeParams(IsOffroad=False), sm).link_maintenance_allowed()
  assert sm.updates == 10
  sm = parked_sample()
  sm["carControl"].latActive = True
  assert not updater_with(FakeParams(IsOffroad=False), sm).link_maintenance_allowed()


def test_updater_waits_while_driving_and_does_not_touch_the_checkout(monkeypatch):
  # A switch to another branch (a pull of the installed branch no longer waits,
  # janpoo6427 2026-11-28; test_link_pull_any_state).
  job = {"operation": "switch", "branch": "next", "branchHead": "b" * 40, "commit": "b" * 40,
         "beforeHead": "a" * 40, "beforeBranch": "main", "phase": "queued"}
  params = FakeParams(IsOffroad=False, **{GIT_UPDATE_KEY: job})
  updater = updater_with(params, None)
  monkeypatch.setattr(updater, "get_commit_hash", lambda path: "a" * 40, raising=False)
  monkeypatch.setattr(updater, "get_branch", lambda path: "main", raising=False)
  monkeypatch.setattr(updater, "link_maintenance_allowed", lambda: False)
  monkeypatch.setattr(updated, "init_overlay", lambda: pytest.fail("must wait for Park or offroad"))
  updater.process_link_update(job)
  assert params.get(GIT_UPDATE_KEY)["phase"] == "queued"


def test_manager_runs_updated_onroad_only_for_a_link_version_change():
  job = {"operation": "switch", "branch": "main", "branchHead": "b" * 40, "commit": "b" * 40,
         "beforeHead": "a" * 40, "beforeBranch": "main"}
  should_run = next(p for p in process_config.procs if p.name == "updated").should_run
  assert should_run(False, FakeParams(), None)
  assert not should_run(True, FakeParams(), None)
  for phase, running in (("queued", True), ("staged", True), ("installed", False), ("failed", False)):
    assert should_run(True, FakeParams(**{GIT_UPDATE_KEY: {**job, "phase": phase}}), None) is running
