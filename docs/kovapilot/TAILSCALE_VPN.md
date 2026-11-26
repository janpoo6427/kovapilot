# 콤마의 Tailscale VPN

## 최초 설치

1. [Tailscale 공식 정적 패키지](https://pkgs.tailscale.com/stable/)에서 버전이 고정된 Linux ARM64 아카이브와 해당 SHA-256을 확보한다.
2. 정차한 콤마에 아카이브를 복사한 뒤 kovapilot 루트에서 Gateway와 같은 사용자로 설치한다.

   ```bash
   bash scripts/install_kova_tailscale.sh /path/to/tailscale_VERSION_arm64.tgz EXPECTED_SHA256
   ```

3. [KovaLink Gateway 소유자 인증](KOVALINK_GATEWAY.md)을 구성하고 같은 Wi-Fi 또는 핫스팟에서 KovaLink와 콤마를 등록한다.
4. tailnet에서 [MagicDNS와 HTTPS](https://tailscale.com/docs/how-to/set-up-https-certificates)를 설정하고 [접근 정책](https://tailscale.com/docs/features/access-control/grants)으로 소유자 기기에서 이 노드의 TCP 443에 접근하도록 허용한다. 공개 인증서 발급 시 노드 이름은 Certificate Transparency 로그에 남는다.
5. 콤마 시동을 끈 상태에서 KovaLink의 **장치 연결 → Tailscale VPN → Tailscale 연결**을 선택한다. **브라우저에서 로그인**이 표시되면 누른 뒤 공식 Tailscale 페이지에서 장치 등록을 완료한다. tailnet에 장치 승인 정책이 있으면 관리자 화면의 승인도 필요하다.
6. 휴대폰의 Tailscale 앱을 같은 tailnet에 연결하고 KovaLink에서 **VPN으로 상태 보기**를 선택한다.

## 동작 범위

- Gateway가 소유자 인증 구성과 설치된 바이너리를 확인하면 `tailscaled`를 userspace 모드로 실행한다. 실행 파일과 노드 상태는 `/persist/kova/tailscale`에 있고, 기존 Wi-Fi 서비스는 자체 바인딩을 사용한다. 콤마는 exit node나 서브넷 경로를 광고하지 않으며 Tailscale DNS와 서브넷 경로를 적용하지 않는다.
- 등록과 해제는 기존 Wi-Fi TLS/소유자 인증 및 offroad 판정을 거친다. KovaLink는 Tailscale 로그인 URL을 시스템 브라우저로 넘긴다.
- Tailscale Serve는 별도 loopback 포트의 `GET /v1/vpn/device`만 제공한다. 클라이언트는 페어링된 콤마에서 확인한 노드 DNS 이름과 일반 TLS 인증서 검증을 사용한다. VPN 요청에는 소유자 토큰에서 파생된 읽기 전용 토큰을 사용한다. 장치 버전, 브랜치, 커밋과 현재 주행 정보 스냅샷을 응답한다.
- 연결 해제는 Wi-Fi의 소유자 연결에서 수행할 수 있다. 장치에 접근할 수 없으면 Tailscale 관리자 콘솔의 Machines에서 해당 노드의 권한을 회수할 수 있다.

정차 벤치에서 모바일 데이터 접속, 정상 재부팅, AGNOS 슬롯 전환, 권한 회수 후 접속 거절, 기존 Wi-Fi 접속 유지 여부를 각각 확인한다.
