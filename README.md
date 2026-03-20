# MCP Bridge — Homelab Network Bridge Server

Claude.ai 웹 채팅의 egress 제약을 해소하기 위한 경량 MCP(Model Context Protocol) 서버.
Cloudflare Tunnel을 통해 Claude.ai에서 홈랩 내부망 API와 외부 URL에 접근할 수 있게 합니다.

## 제공 Tool

| Tool | 설명 |
|------|------|
| `http_fetch` | 범용 HTTP 요청 (외부 URL fetch + 홈랩 API 호출) |
| `dns_lookup` | DNS 레코드 조회 (dig 기반) |
| `whois_lookup` | WHOIS 조회 |
| `ping` | ICMP ping |
| `traceroute` | UDP traceroute |
| `port_check` | TCP 포트 연결 테스트 |

## 설치

### 방법 1: deb 패키지 (권장)

```bash
# 빌드 머신에서 패키지 생성
make deb

# LXC로 복사 후 설치
scp build/mcp-bridge_1.0.0_all.deb root@<LXC_IP>:/tmp/
ssh root@<LXC_IP> "dpkg -i /tmp/mcp-bridge_1.0.0_all.deb; apt-get install -f -y"
```

패키지가 자동으로 수행하는 작업:
- `server.py`, `requirements.txt` → `/opt/mcp-bridge/`
- `mcp-bridge.service` → `/lib/systemd/system/`
- 환경변수 파일 → `/etc/default/mcp-bridge`
- venv 생성 및 Python 의존성 설치 (`fastmcp`, `aiohttp`)
- ping capability 설정
- systemd 서비스 등록 및 시작

### 방법 2: 수동 설치

```bash
# 패키지 설치
apt update && apt install -y python3 python3-venv dnsutils iputils-ping traceroute whois

# 파일 배치
mkdir -p /opt/mcp-bridge
cp server.py requirements.txt /opt/mcp-bridge/
cp mcp-bridge.service /lib/systemd/system/

# venv 생성 및 Python 의존성 설치
python3 -m venv /opt/mcp-bridge/venv
/opt/mcp-bridge/venv/bin/pip install -r requirements.txt

# 서비스 시작
systemctl daemon-reload
systemctl enable --now mcp-bridge
```

## 설정

### 인증 토큰

```bash
# /etc/default/mcp-bridge 편집
MCP_AUTH_TOKEN=your-secret-token-here

# 서비스 재시작
systemctl restart mcp-bridge
```

토큰을 비워두면 인증이 비활성화됩니다 (개발/테스트 용도).

### LXC 스펙 (Proxmox)

```bash
pct create <VMID> local:vztmpl/ubuntu-24.04-standard_*.tar.zst \
  --hostname mcp-bridge \
  --cores 1 --memory 512 --swap 256 \
  --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,tag=20,ip=dhcp \
  --unprivileged 1 --features nesting=1
```

| 항목 | 값 |
|------|-----|
| OS | Ubuntu 24.04 |
| vCPU | 1 |
| RAM | 512MB |
| Disk | 4GB |
| 타입 | unprivileged |
| 네트워크 | VLAN 20 (internal) |

## Cloudflare Tunnel 연결

기존 `cloudflared` 설정에 추가:

```yaml
- hostname: mcp.yourdomain.com
  service: http://localhost:8080
```

Claude.ai에서 등록:
**Settings → Integrations → Add MCP Server** → URL: `https://mcp.yourdomain.com/sse`

## 사용 예시

Claude.ai 웹 채팅에서 자연어로 요청:

```
"https://example.com 내용 가져와줘"              → http_fetch
"Proxmox VM 목록 조회해줘"                       → http_fetch (Proxmox API)
"google.com MX 레코드 조회해줘"                  → dns_lookup
"10.0.0.1 포트 22,80,443 열려있는지 확인해줘"     → port_check
"8.8.8.8 ping 해봐"                             → ping
"google.com traceroute 해줘"                     → traceroute
"example.com WHOIS 조회해줘"                     → whois_lookup
```

## Claude Code (stdio 모드)

로컬 Claude Code에서 사용하려면 `~/.claude.json` 또는 프로젝트의 `.mcp.json`에 추가:

```json
{
  "mcpServers": {
    "homelab-bridge": {
      "command": "python3",
      "args": ["/opt/mcp-bridge/server.py"],
      "env": {
        "MCP_AUTH_TOKEN": ""
      }
    }
  }
}
```

> stdio 모드에서는 FastMCP가 자동으로 transport를 감지합니다.

## 보안

- **SSRF 방어**: metadata endpoint(169.254.x.x), loopback(127.x.x.x) 차단
- **Command Injection 방어**: 호스트명/도메인 입력을 정규식 화이트리스트로 검증
- **HTTP 메서드 제한**: GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS만 허용
- **응답 헤더 필터링**: Set-Cookie 등 민감한 헤더 제거
- **입력 범위 제한**: timeout(1~60s), ping count(1~20), max_hops(1~30), 포트 수(~100)

## OPNsense 방화벽 규칙

| Source | Dest | Port | 용도 |
|--------|------|------|------|
| mcp-bridge IP | Proxmox host | 8006/tcp | Proxmox API |
| mcp-bridge IP | OPNsense | 443/tcp | OPNsense API |
| mcp-bridge IP | Zabbix VM | 80/tcp | Zabbix API |
| mcp-bridge IP | any external | 80,443/tcp | 외부 fetch |
| mcp-bridge IP | any | 53/udp, 43/tcp | DNS, WHOIS |
| mcp-bridge IP | any | icmp | ping |

## 관리

```bash
# 서비스 상태 확인
systemctl status mcp-bridge

# 로그 확인
journalctl -u mcp-bridge -f

# 재시작
systemctl restart mcp-bridge

# 패키지 제거
apt remove mcp-bridge        # 설정 파일 유지
apt purge mcp-bridge         # 설정 파일 포함 제거
```

## 파일 구조

```
/opt/mcp-bridge/
├── server.py               # MCP 서버 본체
├── requirements.txt        # Python 의존성
└── venv/                   # Python 가상환경 (postinst에서 자동 생성)

/lib/systemd/system/
└── mcp-bridge.service      # systemd 유닛

/etc/default/
└── mcp-bridge              # 환경변수 (MCP_AUTH_TOKEN)
```
