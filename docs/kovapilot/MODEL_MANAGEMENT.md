# 모델 관리

## 저장 구조

Git에는 모델 본문이 아니라 Git LFS 포인터가 저장된다. 포인터는 표준 버전 문자열, SHA-256 OID, 파일 크기를 포함한다. 실제 객체는 `.git/lfs/objects/<2>/<2>/<sha256>`에 캐시되고 `lfs.py`가 다운로드·해시 검증·materialize를 담당한다.

저장소 역할은 다음과 같이 분리한다.

- GitHub `kovapilot`: 소스와 LFS 포인터
- `commaai/openpilot-lfs`: 순정 객체의 공개 다운로드 원본. 업로드 대상이 아니다.
- `KOVAPILOT_LFS_BUCKET`: 향후 커스텀 객체의 다운로드 fallback 및 유일한 업로드 대상

현재 `.gitattributes`는 ONNX, 일부 tinygrad PKL, NPZ와 UI 자산 등도 LFS로 관리한다. 따라서 모델 파일만이 아니라 새 LFS 대상 파일 전체가 같은 정책을 따른다.

## 환경 변수

| 변수 | 의미 | 기본값 |
|---|---|---|
| `OPENPILOT_LFS_BUCKET` | 순정/기본 다운로드 버킷 | `commaai/openpilot-lfs` |
| `KOVAPILOT_LFS_BUCKET` | 커스텀 다운로드 fallback 및 업로드 버킷 | 미설정 |
| `HF_TOKEN` 또는 `HUGGING_FACE_HUB_TOKEN` | 커스텀 업로드 토큰 | 미설정 |
| `HF_HUB_DISABLE_IMPLICIT_TOKEN` | `1`이면 로컬 HF 토큰 자동 사용 금지 | 미설정 |
| `GIT_LFS_SKIP_PUSH` | 긴급 상황에서 pre-push LFS 처리를 명시적으로 생략 | 미설정 |
| `GIT_LFS_SKIP_SMUDGE` | checkout 시 객체 materialize 생략 | 미설정 |

`OPENPILOT_LFS_BUCKET`은 기존 설정과 호환되는 다운로드 원본 선택 변수다. 원본 저장소 보호를 위해 업로드에는 사용하지 않는다.

## 설치와 다운로드

Linux/WSL2 또는 macOS 저장소 루트에서 다음을 실행한다.

```bash
./lfs.py install
./lfs.py pull
```

`install`은 시스템 `git-lfs` hook을 프로젝트의 `lfs.py` hook으로 바꾸고 filter-process를 설정한다. `pull`은 인덱스의 포인터를 읽어 순정 버킷에서 먼저 다운로드하고, 객체가 없고 `KOVAPILOT_LFS_BUCKET`이 설정되어 있으면 커스텀 버킷을 시도한다. 다운로드 후 크기와 SHA-256이 포인터와 일치해야 캐시에 반영된다.

현재 `lfs.py`는 POSIX `fcntl`을 사용하므로 Windows PowerShell에서 직접 실행하지 말고 WSL2에서 실행한다.

## Push 정책

pre-push는 이번 Push에 새로 도달 가능한 LFS 포인터만 조사한다.

1. 모든 객체가 `OPENPILOT_LFS_BUCKET`에 존재하면 업로드와 HF 로그인을 건너뛰고 Git Push를 계속한다.
2. 원본에 없는 객체가 있고 `KOVAPILOT_LFS_BUCKET`이 설정되어 있으면 그 객체만 커스텀 버킷에 업로드한다.
3. 원본에 없는 객체가 있는데 커스텀 버킷이 없으면 깨진 포인터의 Push를 막고 설정 방법을 출력한다.
4. 업로드 전 로컬 객체의 크기와 SHA-256을 다시 검증하고, 업로드 후 원격 크기를 확인한다.

커스텀 버킷이 준비된 뒤에만 다음을 설정한다.

```bash
export KOVAPILOT_LFS_BUCKET='<USERNAME>/kovapilot-lfs'
export HF_TOKEN='<write-token>'
git push origin <branch>
```

토큰을 저장소 파일이나 `.env` 커밋에 넣지 않는다. 일반 소스 변경 Push에는 `KOVAPILOT_LFS_BUCKET`과 토큰이 필요 없다.

## 장애 복구

- 파일이 작은 텍스트 포인터로 남음: `./lfs.py pull`을 다시 실행한다.
- `LFS object ... was not found`: `OPENPILOT_LFS_BUCKET` 값과 커스텀 버킷 이름을 확인한다.
- `new LFS objects ...`: 커스텀 버킷을 만들고 환경 변수를 설정하거나, 의도하지 않은 LFS 파일을 커밋에서 제거한다.
- 401/403: 커스텀 버킷에 대한 write 권한과 `HF_TOKEN`을 확인한다.
- 해시 불일치: 해당 `.git/lfs/objects` 객체를 제거한 뒤 pull한다. 포인터 OID를 임의로 수정하지 않는다.
- 시스템 `git-lfs` hook이 실행됨: `./lfs.py install` 후 `git config --get filter.lfs.process`와 `.git/hooks/pre-push`를 확인한다.

실제 업로드는 버킷과 인증 정보가 준비된 경우에만 수행한다.
