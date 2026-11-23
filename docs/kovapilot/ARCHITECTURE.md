# kovapilot 아키텍처

## 목표와 경계

kovapilot은 openpilot 최신 구조를 가능한 그대로 추적하면서, 프로젝트 고유 기능을 `openpilot/kovapilot/`에 격리한다. 현재 단계에서는 패키지 경계와 모델 관리 정책만 만들며 MADS, 종방향 제어, CAN 송신, panda safety 동작은 변경하지 않는다.

통합 원칙은 다음과 같다.

1. 순정 파일 변경은 작은 연결 지점으로 제한한다.
2. 커스텀 정책은 순정 프로세스에서 직접 전개하지 않고 `bridges`의 좁은 인터페이스를 거친다.
3. `contracts`는 양쪽이 공유하는 데이터 형태만 소유하며, cereal 스키마 변경은 필요성이 입증될 때 별도로 검토한다.
4. 순정 planner, controller, safety 및 driver-monitoring의 제한 조건을 우회하지 않는다.
5. 기능은 기본 비활성 상태에서 단위 테스트와 오프라인 replay를 먼저 통과해야 한다.

## 디렉터리 구조

```text
openpilot/kovapilot/
├── contracts/       # 모듈 간 안정적인 입력/출력 계약
├── engagement/      # 향후 MADS 상태 및 운전자 이벤트 정책
├── longitudinal/    # 향후 감속 후보와 승차감 정책
├── navigation/      # 외부 내비 데이터 정규화
├── hud/             # HUD 표시 모델과 UI 어댑터 입력
├── vehicle/         # 차종별 기능/튜닝 설정
├── bridges/         # 순정 openpilot 연결 지점
├── config/          # 기능 플래그와 설정 계약
└── tests/           # kovapilot 단위·계약 테스트
```

모든 하위 패키지는 현재 역할 선언만 포함한다. 실제 차량 제어 코드는 추가하지 않았다.

## 의존성 방향

권장 방향은 `openpilot 순정 코드 -> bridges -> contracts/기능 모듈`이다. 기능 모듈은 `controlsd`, `selfdrived`, `LongitudinalPlanner`를 직접 import하지 않고 계약을 통해 입력을 받는다. `vehicle`은 차종별 값만 제공하고, `engagement`와 `longitudinal`은 서로의 내부 상태를 참조하지 않는다.

현재 소스에서 확인한 실행 경계는 다음과 같다.

- `openpilot/selfdrive/selfdrived/selfdrived.py`의 `SelfdriveD`가 `carState`, `pandaStates`, driver monitoring 등으로 engagement 상태와 이벤트를 만들고 `selfdriveState`를 발행한다.
- `openpilot/selfdrive/controls/controlsd.py`의 `Controls`가 `selfdriveState`, `longitudinalPlan`, `modelV2`, `carState`를 구독하고 `LongControl`/lateral controller를 실행해 `carControl`을 발행한다.
- `opendbc_repo/opendbc/car/`의 `CarInterface` 구현이 차량 인식, `CarParams`, CAN parsing 및 차종별 controller/state를 제공한다.
- `openpilot/selfdrive/pandad/pandad.cc`가 cereal의 `sendcan`을 panda 장치로 보내고 수신 CAN과 `pandaStates`를 발행한다. 실제 safety 구현은 `panda` submodule에 있다.
- `openpilot/cereal/`의 스키마와 `openpilot.cereal.messaging`이 위 프로세스의 메시지 계약을 제공하고, 전송 구현은 `msgq_repo`에 의존한다.
- `rednose_repo`: 상태 추정 관련 코드
- `teleoprtc_repo`: 실시간 통신
- `tinygrad_repo`: 모델 실행 의존성

`cereal`은 현재 메인 저장소의 `openpilot/cereal/`에 있으며 새 메시지가 꼭 필요한 경우에만 변경한다. safety 또는 실제 조향·가감속 명령 생성 변경은 별도 안전 설계와 검증 없이는 허용하지 않는다.

2026-11-23 기준 기반 commit은 `af90646a4a02a93ab48d34a3fc017a9414ac79b9`이다. 이 commit의 공식 차량 목록과 `opendbc_repo` Hyundai 플랫폼에는 IONIQ 9 항목이 없으므로 현재 공식 지원 차량으로 가정하지 않는다. 반면 chestnut 탐지와 big-model 실행 경로는 `openpilot/common/hardware/usb.py`, `openpilot/selfdrive/modeld/helpers.py`, `modeld.py`에 존재한다. 차량 지원과 하드웨어 실행 검증은 서로 별개다.
