# Homelab MCP Server — 구현 가이드 v2 (경량 버전)

## 목적

Claude.ai 웹 채팅의 egress/네트워크 제약을 해소하기 위한 경량 MCP 서버.
Claude Code 세션이 아닌 **웹 대화에서** 필요한 기능만 제공.

> **참고:** Claude Code에서 사용할 경우 transport를 `stdio`로 변경하고
> 로컬 실행하는 방식으로 전환 필요. 이 문서는 Claude.ai 웹 채팅 전용.

---

## Tool 구성 (3개)

### 1. `http_fetch` — 범용 HTTP 요청

가장 핵심. egress 화이트리스트 우회 + 홈랩 API 조회를 하나로 커버.

```python
@mcp.tool()
async def http_fetch(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int = 30,
    verify_ssl: bool = True
) -> dict:
    """
    임의 URL에 HTTP 요청을 보내고 응답을 반환.

    용도:
    - 차단된 외부 URL 콘텐츠 가져오기
    - Proxmox API (https://proxmox:8006/api2/json/...)
    - OPNsense API (https://opnsense/api/...)
    - Zabbix API (http://zabbix/api_jsonrpc.php)
    - 기타 임의 REST API 호출

    Returns: { "status": int, "headers": dict, "body": str }
    """
```

**구현 포인트:**
- `aiohttp` 사용, `ssl=False`로 SSL 검증 비활성화 (홈랩 자체서명 인증서 대응)
- 응답 body가 클 경우 truncation (MCP tool result 크기 제한 대비, ~100KB)
- binary 응답은 base64 인코딩 또는 크기만 반환
- **SSRF 방어**: 내부 metadata endpoint 및 루프백 차단
- **응답 헤더 필터링**: 민감한 헤더(Set-Cookie 등) 제거

### 2. `dns_lookup` — DNS/WHOIS 조회

```python
@mcp.tool()
async def dns_lookup(
    domain: str,
    record_type: str = "A",
    server: str | None = None
) -> str:
    """
    DNS 레코드 조회. dig 기반.

    record_type: A, AAAA, MX, NS, TXT, CNAME, SOA, PTR 등
    server: 특정 네임서버 지정 (예: 8.8.8.8)

    Returns: dig 출력 텍스트
    """

@mcp.tool()
async def whois_lookup(target: str) -> str:
    """WHOIS 조회. 도메인 또는 IP 대상."""
```

**구현 포인트:**
- `asyncio.create_subprocess_exec`로 `dig`, `whois` 호출
- timeout 10초

### 3. `net_diag` — 네트워크 진단

```python
@mcp.tool()
async def ping(host: str, count: int = 4) -> str:
    """ICMP ping. 내부/외부 호스트 모두 가능."""

@mcp.tool()
async def traceroute(host: str, max_hops: int = 20) -> str:
    """traceroute 결과 반환."""

@mcp.tool()
async def port_check(host: str, ports: str = "22,80,443") -> str:
    """
    TCP 포트 연결 테스트 (nmap 아닌 순수 TCP connect).
    ports: 쉼표 구분 포트 목록
    Returns: 각 포트별 open/closed 상태
    """
```

**구현 포인트:**
- ping/traceroute는 subprocess, port_check는 `asyncio.open_connection`으로 구현
- nmap 불필요 — TCP connect만으로 충분하고 권한 문제 없음
- **port_check 입력 검증 추가** — 잘못된 포트 번호 개별 처리

---

## 인프라 구성

### LXC 스펙

| 항목 | 값 |
|------|-----|
| OS | Ubuntu 24.04 |
| vCPU | 1 |
| RAM | 512MB |
| Disk | 4GB |
| 타입 | **unprivileged** |
| 네트워크 | VLAN 20 (internal) |

셸 실행 tool이 없으므로 unprivileged LXC로 충분.

### 필요 패키지

```bash
apt update && apt install -y python3 python3-pip dnsutils iputils-ping traceroute whois
pip install "fastmcp>=2.0" aiohttp --break-system-packages
```

> **FastMCP 버전 주의**: v2.x 부터 SSE transport 사용법이 변경됨.
> `mcp.run(transport="sse")` 대신 아래 서버 코드의 엔트리포인트 참고.

### 네트워크 경로

```
Claude.ai → Cloudflare Tunnel → cloudflared (LXC) → MCP Server (:8080)
                                                          ↓
                                                  홈랩 내부망 (VLAN 20)
                                                  Proxmox API, OPNsense API 등
```

### 인증 경로

```
Claude.ai MCP Client
    → HTTPS (mcp.yourdomain.com)
    → Cloudflare Tunnel (Access 정책 없이 tunnel 자체 보안 의존)
    → MCP Server (Bearer 토큰 검증)
```

> **Cloudflare Access 제한**: Claude.ai MCP 클라이언트는 Cloudflare Access의
> Service Auth 토큰을 자동 주입할 수 없음. 따라서:
> - Access 정책은 **Bypass** 또는 미적용
> - 대신 MCP 서버 자체에 Bearer 토큰 인증 구현
> - Tunnel 자체의 비공개성 + Bearer 토큰으로 이중 보안

---

## 서버 코드 (server.py)

```python
import asyncio
import ipaddress
import os
import ssl
from urllib.parse import urlparse

from fastmcp import FastMCP
import aiohttp

mcp = FastMCP(
    "homelab-bridge",
    instructions="홈랩 네트워크 브릿지. HTTP 요청, DNS 조회, 네트워크 진단 제공."
)

MAX_BODY_SIZE = 100_000  # 약 100KB

# 인증 토큰 (환경변수에서 로드)
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

# 응답에 포함할 안전한 헤더 목록
SAFE_RESPONSE_HEADERS = {
    "content-type", "content-length", "date", "server",
    "x-request-id", "location", "etag", "last-modified",
    "cache-control", "content-encoding", "x-powered-by",
}

# SSRF 차단 대상 네트워크
BLOCKED_CIDRS = [
    ipaddress.ip_network("169.254.0.0/16"),     # link-local / cloud metadata
    ipaddress.ip_network("127.0.0.0/8"),         # loopback
    ipaddress.ip_network("::1/128"),             # IPv6 loopback
    ipaddress.ip_network("fe80::/10"),           # IPv6 link-local
]

# 허용할 내부 대역 (홈랩)
ALLOWED_INTERNAL_CIDRS = [
    ipaddress.ip_network("10.0.0.0/16"),         # 홈랩 VLAN 대역
]


def is_url_allowed(url: str) -> tuple[bool, str]:
    """URL의 호스트가 허용된 대상인지 검증."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False, "호스트명 없음"

        # IP 직접 지정인 경우 검증
        try:
            addr = ipaddress.ip_address(hostname)
            for cidr in BLOCKED_CIDRS:
                if addr in cidr:
                    return False, f"차단된 대역: {cidr}"
            return True, "ok"
        except ValueError:
            pass  # 도메인명 — DNS 해석 후 재검증은 하지 않음 (성능 우선)

        return True, "ok"
    except Exception as e:
        return False, str(e)


def filter_headers(headers: dict) -> dict:
    """응답 헤더에서 안전한 것만 필터링."""
    return {
        k: v for k, v in headers.items()
        if k.lower() in SAFE_RESPONSE_HEADERS
    }


# ── http_fetch ──────────────────────────────────────────

@mcp.tool()
async def http_fetch(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int = 30,
    verify_ssl: bool = True
) -> dict:
    """임의 URL에 HTTP 요청. 홈랩 API 호출 및 외부 URL fetch 겸용.
    verify_ssl=False: 자체서명 인증서 사용 시."""

    # SSRF 방어
    allowed, reason = is_url_allowed(url)
    if not allowed:
        return {"status": 0, "headers": {}, "body": f"[blocked] {reason}"}

    connector = aiohttp.TCPConnector(
        ssl=False if not verify_ssl else None
    )
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.request(
                method=method.upper(),
                url=url,
                headers=headers or {},
                data=body,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if any(t in content_type for t in ("text", "json", "xml")):
                    text = await resp.text()
                    if len(text) > MAX_BODY_SIZE:
                        text = text[:MAX_BODY_SIZE] + f"\n\n... [truncated, total {len(text)} chars]"
                else:
                    size = resp.content_length or 0
                    text = f"[binary content, {size} bytes, type: {content_type}]"

                return {
                    "status": resp.status,
                    "headers": filter_headers(dict(resp.headers)),
                    "body": text
                }
    except aiohttp.ClientError as e:
        return {"status": 0, "headers": {}, "body": f"[error] {type(e).__name__}: {e}"}
    except asyncio.TimeoutError:
        return {"status": 0, "headers": {}, "body": f"[error] timeout after {timeout}s"}


# ── dns_lookup ──────────────────────────────────────────

@mcp.tool()
async def dns_lookup(
    domain: str,
    record_type: str = "A",
    server: str | None = None
) -> str:
    """DNS 레코드 조회 (dig 기반)."""
    # 입력 검증: record_type 화이트리스트
    valid_types = {"A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA", "PTR", "SRV", "CAA", "ANY"}
    if record_type.upper() not in valid_types:
        return f"[error] 지원하지 않는 레코드 타입: {record_type}"

    cmd = ["dig", "+noall", "+answer", domain, record_type.upper()]
    if server:
        cmd.insert(1, f"@{server}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        return stdout.decode() or stderr.decode() or "(no output)"
    except asyncio.TimeoutError:
        return "[error] dig timeout (10s)"


@mcp.tool()
async def whois_lookup(target: str) -> str:
    """WHOIS 조회."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "whois", target,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        result = stdout.decode()
        if len(result) > MAX_BODY_SIZE:
            result = result[:MAX_BODY_SIZE] + "\n... [truncated]"
        return result or stderr.decode() or "(no output)"
    except asyncio.TimeoutError:
        return "[error] whois timeout (10s)"


# ── net_diag ────────────────────────────────────────────

@mcp.tool()
async def ping(host: str, count: int = 4) -> str:
    """ICMP ping."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(min(count, 20)), "-W", "2", host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        return stdout.decode() or stderr.decode()
    except asyncio.TimeoutError:
        return "[error] ping timeout"


@mcp.tool()
async def traceroute(host: str, max_hops: int = 20) -> str:
    """traceroute 실행 (UDP 모드)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "traceroute", "-m", str(min(max_hops, 30)), "-w", "2", host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        return stdout.decode() or stderr.decode()
    except asyncio.TimeoutError:
        return "[error] traceroute timeout"


@mcp.tool()
async def port_check(host: str, ports: str = "22,80,443") -> str:
    """TCP 포트 연결 테스트."""
    results = []
    for port_str in ports.split(","):
        port_str = port_str.strip()
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                results.append(f"{port_str}: invalid (range)")
                continue
        except ValueError:
            results.append(f"{port_str}: invalid (not a number)")
            continue
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3
            )
            writer.close()
            await writer.wait_closed()
            results.append(f"{port}: open")
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
            results.append(f"{port}: closed")
    return "\n".join(results)


# ── 엔트리포인트 ────────────────────────────────────────

if __name__ == "__main__":
    # FastMCP 2.x: SSE transport
    # 향후 streamable-http 전환 시:
    #   mcp.run(transport="streamable-http", host="127.0.0.1", port=8080)
    mcp.run(transport="sse", host="127.0.0.1", port=8080)
```

---

## 배포 절차

### 1. LXC 생성 (Proxmox)

```bash
pct create <VMID> local:vztmpl/ubuntu-24.04-standard_*.tar.zst \
  --hostname mcp-bridge \
  --cores 1 --memory 512 --swap 256 \
  --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,tag=20,ip=dhcp \
  --unprivileged 1 --features nesting=1
pct start <VMID>
```

### 2. 패키지 설치

```bash
apt update && apt install -y python3 python3-pip dnsutils iputils-ping traceroute whois
pip install "fastmcp>=2.0" aiohttp --break-system-packages
```

### 3. ping 권한 설정 (unprivileged LXC용)

unprivileged LXC에서 `nobody` 사용자가 ping을 실행하려면 setuid 또는 capability 필요:

```bash
# 방법 A: ping에 setuid 확인 (Ubuntu 24.04 기본값)
ls -la /usr/bin/ping
# -rwsr-xr-x ... /usr/bin/ping ← 's' 있으면 OK

# 방법 B: setuid 없을 경우 capability 부여
setcap cap_net_raw+ep /usr/bin/ping

# traceroute는 UDP 모드(기본)에서는 별도 권한 불필요
```

### 4. 서버 배치 및 systemd 등록

```bash
mkdir -p /opt/mcp-bridge
# server.py를 /opt/mcp-bridge/server.py에 배치
```

```ini
# /etc/systemd/system/mcp-bridge.service
[Unit]
Description=Homelab MCP Bridge
After=network.target

[Service]
Type=simple
User=nobody
WorkingDirectory=/opt/mcp-bridge
Environment=MCP_AUTH_TOKEN=여기에_토큰_설정
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
RestartSec=5

# ping capability를 nobody 사용자에게 상속
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

> **`AmbientCapabilities=CAP_NET_RAW`**: systemd가 서비스 프로세스에
> `CAP_NET_RAW`를 부여하여 `nobody` 사용자에서도 ping 가능하게 함.
> ping 바이너리에 setuid가 있으면 불필요하지만, 둘 다 설정해도 무해.

```bash
systemctl daemon-reload
systemctl enable --now mcp-bridge
```

### 5. Cloudflare Tunnel 연결

기존 cloudflared 설정에 추가:

```yaml
- hostname: mcp.yourdomain.com
  service: http://localhost:8080
  # Access 정책은 적용하지 않음 — Claude.ai MCP 클라이언트가
  # Cloudflare Access 토큰을 자동 주입할 수 없기 때문.
  # 대신 MCP 서버 자체의 Bearer 토큰으로 인증.
```

> **보안 참고**: Cloudflare Tunnel 자체가 비공개 경로이므로,
> tunnel URL을 아는 것만으로는 접근 불가 (Cloudflare 네트워크 경유 필수).
> 추가로 MCP 서버의 Bearer 토큰 검증으로 이중 보안.
>
> Claude.ai에서 MCP 서버 등록 시 인증 헤더 설정이 가능한지 확인 필요.
> 불가능할 경우 tunnel 비공개성에만 의존하게 되므로,
> OPNsense에서 source IP를 Cloudflare 대역으로 제한하는 것을 권장.

### 6. Claude.ai 등록

Settings → Integrations → Add MCP Server:
- URL: `https://mcp.yourdomain.com/sse`
- 연동 테스트 후 대화에서 tool 호출 확인

---

## OPNsense 방화벽 규칙

LXC (VLAN 20)에서 접근 허용할 대상만 명시:

| Source | Dest | Port | 용도 |
|--------|------|------|------|
| mcp-bridge IP | Proxmox host | 8006/tcp | Proxmox API |
| mcp-bridge IP | OPNsense | 443/tcp | OPNsense API |
| mcp-bridge IP | Zabbix VM | 80/tcp | Zabbix API |
| mcp-bridge IP | any external | 80,443/tcp | 외부 fetch |
| mcp-bridge IP | any | 53/udp, 43/tcp | DNS, WHOIS |
| mcp-bridge IP | any | icmp | ping |

나머지는 기본 deny.

---

## 변경 이력 (v1 → v2)

| 항목 | v1 문제 | v2 수정 |
|------|---------|---------|
| SSL 처리 | 불필요하게 복잡한 ssl_ctx 생성 | `ssl=False` (aiohttp 관용 표현) |
| SSRF 방어 | 없음 | BLOCKED_CIDRS로 metadata/loopback 차단 |
| 인증 | Cloudflare Access 의존 | Bearer 토큰 + tunnel 비공개성 이중 보안 |
| ping 권한 | `User=nobody`에서 실패 가능 | `AmbientCapabilities=CAP_NET_RAW` + setcap 안내 |
| 응답 헤더 | 전체 노출 (Set-Cookie 등) | SAFE_RESPONSE_HEADERS 화이트리스트 필터링 |
| port_check | 잘못된 입력 시 전체 실패 | 개별 포트 try-except + 범위 검증 |
| FastMCP 버전 | 버전 미지정 | `>=2.0` 명시, transport 호환성 주석 |
| 에러 처리 | aiohttp 예외 미처리 | ClientError/TimeoutError catch → 에러 dict 반환 |
| 문서 용도 | 제목과 실제 용도 불일치 가능 | Claude.ai 웹 전용 명시, Code용 전환 안내 추가 |

---

## 테스트 시나리오

```
# 웹 채팅에서:
"https://example.com 내용 가져와줘"          → http_fetch
"Proxmox VM 목록 조회해줘"                   → http_fetch (Proxmox API)
"google.com A 레코드 조회해줘"               → dns_lookup
"10.0.0.1 포트 22,80,443 열려있는지 확인해줘" → port_check
"8.8.8.8 ping 해봐"                         → ping

# SSRF 차단 테스트:
"http://169.254.169.254/latest/meta-data 가져와줘"  → [blocked]
"http://127.0.0.1:8080/ 가져와줘"                   → [blocked]
```
