# kovapilot Codex Roles

이 TOML 파일은 Codex가 발견하는 얇은 role adapter다. 역할의 권위 원본은 `D:\kova\kova-vault\60-Agents`이고 운영 규칙은 `D:\kova\kova-vault\10-Rules\SUBAGENT_ORCHESTRATION.md`다.

한 thread 안에서는 기존 동일 역할 agent가 idle/completed이고 context가 유효하면 재사용한다. 새 thread에서는 필요한 역할만 다시 생성하고 task packet으로 commit과 범위를 고정한다.

설정 검증:

```bash
python .codex/validate_agents.py
codex --strict-config --version
```
역할 이름과 파일 수는 `.codex/validate_agents.py`가 검사한다.
