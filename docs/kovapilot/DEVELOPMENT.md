# 개발 환경과 작업 흐름

## 최초 설정

이 저장소는 openpilot 메인 저장소와 6개 submodule을 사용한다. Clone 직후 Linux/WSL2 또는 macOS에서 다음 순서로 준비한다.

```bash
git submodule update --init --recursive
tools/op.sh setup
./lfs.py pull
```

`tools/op.sh setup`은 현재 upstream이 제공하는 통합 설정 경로이며 submodule, OS 의존성, uv 기반 `.venv`, 프로젝트 LFS filter를 준비한다. 완료 후 새 shell에서 `op` 명령을 사용할 수 있다.

## Windows WSL2 Ubuntu 24.04

openpilot은 Windows 네이티브 개발을 지원하지 않으므로 Ubuntu 24.04 WSL2 shell에서 작업한다. 저장소를 WSL ext4 영역(예: `~/src/kovapilot`)에 두는 편이 파일 감시와 빌드 성능에 유리하다. `/mnt/d` checkout을 사용할 경우에도 모든 setup/build/test 명령은 WSL shell에서 실행한다.

```bash
cd ~/src/kovapilot
tools/op.sh setup
op build -j4
```

## macOS ARM64

현재 `tools/op.sh`가 macOS를 감지하고 같은 setup 흐름을 제공한다.

```bash
cd ~/src/kovapilot
tools/op.sh setup
op build -j4
```

하드웨어 전용 daemon이나 Linux 전용 통합 테스트는 macOS에서 실행되지 않을 수 있다. 이 경우 단위 테스트 성공과 comma 장치/리플레이 검증을 구분해서 기록한다.

## 테스트

환경 활성화 후 kovapilot 범위만 실행한다.

```bash
source .venv/bin/activate
python -c "import openpilot.kovapilot"
pytest -q openpilot/kovapilot/tests
```

전체 build는 `op build`를 사용한다. 차량 제어 변경이 시작되면 관련 순정 테스트, opendbc 차량 테스트, panda safety 테스트, offline replay를 서로 별도 증거로 남긴다.

## 브랜치와 upstream 동기화

현재 `up-openpilot`은 공식 `commaai/openpilot`, `origin`은 kovapilot 저장소이며 `up-carrot`은 비교 분석용 `ajouatom/openpilot`이다. remote URL을 바꾸지 않고 다음 흐름을 권장한다.

```bash
git fetch up-openpilot
git switch master
git merge --ff-only up-openpilot/master
git switch -c feature/<topic>
```

기능 브랜치에서 커밋하고 검증한 뒤 `origin`으로 Push한다. upstream 갱신 전에는 dirty 상태와 submodule 포인터 변경을 확인한다. 순정 파일과의 연결 지점을 작게 유지하고, 충돌 해결 과정에서 upstream 변경을 덮어쓰지 않는다.

## 현재 안전 경계

아직 MADS, longitudinal planner/controller, CAN 송신, panda safety, driver monitoring을 수정하거나 활성화하지 않는다. 실제 차량 검증 전에는 새 동작을 기본 비활성으로 유지한다.
