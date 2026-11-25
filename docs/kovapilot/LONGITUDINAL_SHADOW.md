# 종방향 감속 후보 관측

`replay_shadow`는 Linux에서 로컬 full rlog의 `modelV2`, `carState`, `carControl`, `controlsState`, `carParams`를 시간순으로 읽는 오프라인 명령이다. 별도 명령을 실행할 때만 JSONL을 출력한다. 차량 프로세스와 연결되는 메시지 발행이나 제어 쓰기 경로가 없다.

```bash
python -m openpilot.kovapilot.longitudinal.replay_shadow /path/to/rlog.zst > /path/to/shadow.jsonl
```

모델 프레임마다 `model_mono_time_ns`, `reason`, `cruise_speed_mps`, `candidate`, `selected`를 기록한다. `selected`는 후보가 운전자 설정속도보다 낮았다는 뜻이다. 후보에는 `source`, `speed_mps`, `observed_at_ns`가 있다. `model_mono_time_ns`는 원본 `longitudinalPlan.modelMonoTime`과 비교할 때 사용할 수 있다. JSONL에는 route 식별자와 위치를 기록하지 않는다.

첫 후보 `model_curve`는 향후 6초간 모델의 종방향 속도와 yaw rate에서 곡률을 추정하고, 횡가속도 관측 기준 2.8 m/s²에 해당하는 속도를 계산한다. 이는 오프라인 비교용 기준값이다. 직접 종방향 지원, 유효한 모델과 차량 상태, 활성 종방향 제어, 초기화된 설정속도를 요구한다. 페달·CANCEL 개입, 순정 ACC, 강제 감속, 유실 또는 250 ms보다 오래된 상태에서는 후보 선정을 건너뛴다. 세그먼트 중간에 `carParams`가 처음 나오면 그 전 프레임은 `longitudinal_unavailable`로 기록한다. 이전 로그의 설정속도는 기존 process replay의 `migrate_carState`로 읽는다.

EV6 기준선을 재현하려면 `opendbc_repo/opendbc/car/tests/routes.py`의 세 번째 `HYUNDAI.KIA_EV6` 공개 route에서 full rlog 0번과 2번을 사용한다. 원본 `carParams.openpilotLongitudinalControl`, `carControl.longActive`를 확인한 뒤 `replay_process_with_name`으로 `plannerd`와 `controlsd`를 각각 재생할 수 있다. shadow 유무를 비교할 때 `logMonoTime`, `longitudinalPlan.processingDelay`, `longitudinalPlan.solverExecutionTime`은 실행 타이밍 필드로 제외한다.

## 내비 카메라 프로필 리플레이

`--navigation`은 순정과 여러 외부 제공자에서 정규화한 카메라 JSONL을 읽는다. `--kova-params`는 재현할 설정의 `KovaParams` 상태를 읽는다. 둘 다 로컬 파일이며 기존 `plannerd`·`controlsd` 출력은 그대로 사용한다.

```bash
python -m openpilot.kovapilot.longitudinal.replay_shadow /path/to/rlog.zst \
  --navigation /path/to/cameras.jsonl --kova-params /path/to/kova_params_state.json > /path/to/shadow.jsonl
```

설정 상태 예시

```json
{"schemaVersion": 1, "revision": 0, "values": {"navigation.speed_camera_profile": 2}}
```

`0` 사용 안 함이 기본값이다. `1` 여유는 첫 수신부터 감속하며 필요 거리 여유가 있으면 접근의 첫 1/4 지점에 목표속도 도달을 계획한다. `2` 균형은 최초 남은 거리의 절반, `3` 근접은 직전 여유 거리에서 목표속도에 도달한다. 직전 여유는 최소 20m와 목표속도로 2초 주행하는 거리 중 큰 값으로 계산한다. 공통 감속도·jerk 한계 내에서 목표를 맞추기 어려우면 실제 계산된 도달 예상 지점을 별도로 기록한다. 통과는 별도의 `passed` 입력으로 확인하며 그 이후에만 프로필별 속도로 상한을 해제한다. 여유의 1/4 지점 및 감속·복귀 수치는 초기 오프라인 검증값이다.

카메라 입력의 한 줄 예시

```json
{"source":"vehicle","session":"a","event_id":"camera_1","sequence":1,"observed_ns":100000000000,"received_ns":100000000000,"state":"present","road_match":true,"distance_m":400,"target_mps":16.67}
```

- `source`는 `vehicle`, `external_a`처럼 개인 정보가 없는 제공자 별칭을 사용한다. 같은 카메라는 소스가 달라도 같은 정규화 `event_id`를 쓰고, 이후 별개 접근에는 새 이벤트 ID를 쓴다. 도로 일치 여부와 중복 매칭은 입력을 정규화하는 단계에서 판단한다.
- 두 시각은 rlog와 같은 단조 시계 기준이다. `observed_ns`는 재전송이 업데이트하지 않는 최초 원본 관측 시각, `received_ns`는 리플레이에서 행을 전달할 시각이다. 마지막 유효 관측이 2초를 넘으면 후보를 해제한다. 재전송은 수명이나 절반 기준을 연장하지 않는다.
- `present`는 거리와 목표속도를 요구한다. `clear`는 해당 소스의 이벤트만 제거하며 거리·속도를 생략할 수 있다. `passed`는 확인된 통과를 의미하고 0 이하의 거리를 요구한다. 이탈이나 데이터 만료는 통과와 구분한다.
- `navigation` 결과에는 카메라별 상태, 제외 사유, 목표·예상·원래 로그에서 관측한 도달 거리와 원래 로그의 목표속도 초과 시간이 포함된다. 카메라 상한은 기존 모델 곡선 상한과 비교한다. 기존 제어 조건이 충족될 때만 `selected_candidate`를 기록한다. `--navigation` 생략 시 기존 JSONL 형식을 사용한다.

## 실제 플래너 연결과 입력 요건

`plannerd`는 새 주행 세션에서 `KovaParams`의 offroad 설정을 읽는다. 지원 차량의 종방향이 활성이고 옵션 `1`~`3`을 설정한 경우, 로컬 내비 어댑터가 발행하는 `kovaCameraEvents`를 선택적으로 읽는다. 기본값 `0`에서는 구독하지 않는다. 이 내부 주제는 별도 신뢰된 어댑터에서 경로·방향과 단일 지점 카메라를 확인한 뒤 쓰도록 정의했다. `verifiedSinglePoint`와 `roadMatch`가 모두 확인된 데이터만 고려한다. 배치 수신에 실패해도 일반 플래너 메시지 유효성에는 영향이 없다.

유효 카메라 상한은 플래너의 추가 크루즈 감속 후보로 들어간다. 기본 플래너의 선행차·모델 후보와 최소 가속도를 비교하며, 카메라만으로 정차 상태를 만들지 않는다. 기존 감속률과 jerk 한계를 적용한다. 페달·CANCEL·강제 감속에는 기존 경로를 적용한다. 확인된 통과는 프로필별 복귀를 사용하고, 만료로 사라진 상한은 일반 크루즈 jerk 한계 내에서 해제한다. 실제 도달 거리에는 기존 플래너·차량 감속 한계가 영향을 준다.

실시간 모드에서는 카메라 메시지 사이의 남은 거리를 최근 CAN 차량 속도로 보정한다. 원본 거리와 시각은 그대로 사용해 만료와 입력 충돌을 확인한다. 속도 이력이 끊기면 원본 거리를 사용하며, 보정 거리가 0이 돼도 통과로 처리하지 않는다. 감속 시작에 쓰는 이동 거리는 기존 크루즈 감속·jerk 한계, 크루즈 응답 시간, 차량 액추에이터 지연을 반영한다. 거리와 응답의 보정값은 플래너 계산용이며 목표속도 도달 성능은 실제 차량에서 따로 검증한다.

현재 코드는 수신·플래너 경로까지 포함한다. 사용 가능한 순정 CAN 또는 외부 앱의 런타임 발행자는 실제 로그에서 카메라 귀속과 통과 판정을 검증한 뒤 연결한다. 기존 일반 제한속도나 회전 안내만으로 단속 이벤트를 만들지 않는다. 배포 및 차량 활성화는 별도 검증 단계다.
