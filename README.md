# MCP Bridge — Network Utility MCP Server

HTTP 요청, DNS 조회, 네트워크 진단 기능을 제공하는 경량 MCP(Model Context Protocol) 서버.
Claude.ai 웹 채팅 또는 Claude Code에서 네트워크 관련 작업을 수행할 수 있습니다.

## 이게 왜 필요하나요?
가끔 Claude.ai가 fetch 못하는 웹페이지같은걸 읽어들이기 위해 만들었습니다.
항상 Claude Code로 모든것을 처리할수는 없고, 그렇다고 MCP로 쉘을 줄순 없잖아요?
웹페이지 불러온다고 로컬 쉘 세션을 주는 것 만큼 멍청한 짓이 없습니다.
~~모든 것을 Claude에게 맡기기~~

## 제공 Tool

| Tool | 설명 |
|------|------|
| `http_fetch` | 범용 HTTP 요청 (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS) |
| `dns_lookup` | DNS 레코드 조회 (A, AAAA, MX, NS, TXT, CNAME 등) |
| `whois_lookup` | WHOIS 조회 (도메인/IP) |
| `ping` | ICMP ping |
| `traceroute` | UDP traceroute |
| `port_check` | TCP 포트 연결 테스트 |

## 설치

### 방법 1: deb 패키지 (권장)

```bash
# 패키지 빌드
make deb

# 대상 서버로 복사 후 설치
scp build/mcp-bridge_1.0.0_all.deb root@<SERVER_IP>:/tmp/
ssh root@<SERVER_IP> "dpkg -i /tmp/mcp-bridge_1.0.0_all.deb; apt-get install -f -y"
```

패키지가 자동으로 수행하는 작업:
- `server.py`, `requirements.txt` → `/opt/mcp-bridge/`
- `mcp-bridge.service` → `/lib/systemd/system/`
- venv 생성 및 Python 의존성 설치 (`fastmcp`, `aiohttp`)
- ping capability 설정
- systemd 서비스 등록 및 시작

### 방법 2: 수동 설치

```bash
# 시스템 패키지
apt update && apt install -y python3 python3-venv dnsutils iputils-ping traceroute whois

# 파일 배치
mkdir -p /opt/mcp-bridge
cp server.py requirements.txt /opt/mcp-bridge/

# venv 생성 및 Python 의존성 설치
python3 -m venv /opt/mcp-bridge/venv
/opt/mcp-bridge/venv/bin/pip install -r /opt/mcp-bridge/requirements.txt

# systemd 등록
cp mcp-bridge.service /lib/systemd/system/
systemctl daemon-reload
systemctl enable --now mcp-bridge
```

## 연결 방법

### Claude.ai 웹 채팅 (SSE)

서버가 외부에서 접근 가능해야 합니다. 리버스 프록시 또는 Cloudflare Tunnel 등을 통해 노출한 뒤:

**Settings → Integrations → Add MCP Server** → URL: `https://your-domain.com/sse`

### Claude Code (stdio)

`~/.claude.json` 또는 프로젝트의 `.mcp.json`에 추가:

```json
{
  "mcpServers": {
    "mcp-bridge": {
      "command": "/opt/mcp-bridge/venv/bin/python3",
      "args": ["/opt/mcp-bridge/server.py"]
    }
  }
}
```

## 사용 예시

```
"https://example.com 내용 가져와줘"              → http_fetch
"google.com MX 레코드 조회해줘"                  → dns_lookup
"10.0.0.1 포트 22,80,443 열려있는지 확인해줘"     → port_check
"8.8.8.8 ping 해봐"                             → ping
"google.com traceroute 해줘"                     → traceroute
"example.com WHOIS 조회해줘"                     → whois_lookup
```

## 보안

- **SSRF 방어**: link-local(169.254.x.x), loopback(127.x.x.x), IPv6 link-local 차단
- **Command Injection 방어**: 호스트명/도메인 입력을 정규식 화이트리스트로 검증, `-` 시작 차단
- **HTTP 메서드 제한**: GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS만 허용
- **응답 헤더 필터링**: Set-Cookie 등 민감한 헤더 제거
- **입력 범위 제한**: timeout(1~60s), ping count(1~20), max_hops(1~30), 포트 수(최대 100개)
- **응답 크기 제한**: 텍스트 응답 100KB 초과 시 truncation

## 관리

```bash
systemctl status mcp-bridge        # 상태 확인
journalctl -u mcp-bridge -f        # 로그 확인
systemctl restart mcp-bridge       # 재시작

apt remove mcp-bridge              # 제거 (설정 유지)
apt purge mcp-bridge               # 완전 제거
```

## 파일 구조

```
/opt/mcp-bridge/
├── server.py               # MCP 서버 본체
├── requirements.txt        # Python 의존성
└── venv/                   # Python 가상환경 (postinst에서 자동 생성)

/lib/systemd/system/
└── mcp-bridge.service      # systemd 유닛
```
