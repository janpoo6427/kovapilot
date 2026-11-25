# 종방향 감속 후보 관측

`replay_shadow`는 Linux에서 로컬 full rlog의 `modelV2`, `carState`, `carControl`, `controlsState`, `carParams`를 시간순으로 읽는 오프라인 명령이다. 별도 명령을 실행할 때만 JSONL을 출력한다. 차량 프로세스와 연결되는 메시지 발행이나 제어 쓰기 경로가 없다.

```bash
python -m openpilot.kovapilot.longitudinal.replay_shadow /path/to/rlog.zst > /path/to/shadow.jsonl
```

모델 프레임마다 `model_mono_time_ns`, `reason`, `cruise_speed_mps`, `candidate`, `selected`를 기록한다. `selected`는 후보가 운전자 설정속도보다 낮았다는 뜻이다. 후보에는 `source`, `speed_mps`, `observed_at_ns`가 있다. `model_mono_time_ns`는 원본 `longitudinalPlan.modelMonoTime`과 비교할 때 사용할 수 있다. JSONL에는 route 식별자와 위치를 기록하지 않는다.

첫 후보 `model_curve`는 향후 6초간 모델의 종방향 속도와 yaw rate에서 곡률을 추정하고, 횡가속도 관측 기준 2.8 m/s²에 해당하는 속도를 계산한다. 이는 오프라인 비교용 기준값이다. 직접 종방향 지원, 유효한 모델과 차량 상태, 활성 종방향 제어, 초기화된 설정속도를 요구한다. 페달·CANCEL 개입, 순정 ACC, 강제 감속, 유실 또는 250 ms보다 오래된 상태에서는 후보 선정을 건너뛴다. 세그먼트 중간에 `carParams`가 처음 나오면 그 전 프레임은 `longitudinal_unavailable`로 기록한다. 이전 로그의 설정속도는 기존 process replay의 `migrate_carState`로 읽는다.

EV6 기준선을 재현하려면 `opendbc_repo/opendbc/car/tests/routes.py`의 세 번째 `HYUNDAI.KIA_EV6` 공개 route에서 full rlog 0번과 2번을 사용한다. 원본 `carParams.openpilotLongitudinalControl`, `carControl.longActive`를 확인한 뒤 `replay_process_with_name`으로 `plannerd`와 `controlsd`를 각각 재생할 수 있다. shadow 유무를 비교할 때 `logMonoTime`, `longitudinalPlan.processingDelay`, `longitudinalPlan.solverExecutionTime`은 실행 타이밍 필드로 제외한다.
