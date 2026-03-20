import asyncio
import ipaddress
import re
from urllib.parse import urlparse

from fastmcp import FastMCP
import aiohttp

mcp = FastMCP(
    "mcp-bridge",
    instructions="네트워크 유틸리티 MCP 서버. HTTP 요청, DNS 조회, 네트워크 진단 제공.",
)

MAX_BODY_SIZE = 100_000  # 약 100KB

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

# 허용 HTTP 메서드
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# 호스트명/도메인 검증 패턴 (command injection 방어)
HOSTNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._:-]+$")
DOMAIN_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_hostname(value: str) -> tuple[bool, str]:
    """호스트명/IP 입력 검증. subprocess 인자 주입 방어."""
    if not value or len(value) > 253:
        return False, "호스트명이 비어있거나 너무 깁니다"
    if not HOSTNAME_PATTERN.match(value):
        return False, f"허용되지 않는 문자 포함: {value}"
    if value.startswith("-"):
        return False, "호스트명은 '-'로 시작할 수 없습니다"
    return True, "ok"


def validate_domain(value: str) -> tuple[bool, str]:
    """도메인명 검증."""
    if not value or len(value) > 253:
        return False, "도메인이 비어있거나 너무 깁니다"
    if not DOMAIN_PATTERN.match(value):
        return False, f"허용되지 않는 문자 포함: {value}"
    if value.startswith("-"):
        return False, "도메인은 '-'로 시작할 수 없습니다"
    return True, "ok"


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
    verify_ssl: bool = True,
) -> dict:
    """임의 URL에 HTTP 요청. verify_ssl=False: 자체서명 인증서 사용 시."""

    # HTTP 메서드 검증
    if method.upper() not in ALLOWED_METHODS:
        return {"status": 0, "headers": {}, "body": f"[blocked] 허용되지 않는 HTTP 메서드: {method}"}

    # SSRF 방어
    allowed, reason = is_url_allowed(url)
    if not allowed:
        return {"status": 0, "headers": {}, "body": f"[blocked] {reason}"}

    # timeout 범위 제한
    timeout = max(1, min(timeout, 60))

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
                timeout=aiohttp.ClientTimeout(total=timeout),
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
                    "body": text,
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
    server: str | None = None,
) -> str:
    """DNS 레코드 조회 (dig 기반)."""
    # 도메인 입력 검증
    valid, reason = validate_domain(domain)
    if not valid:
        return f"[error] {reason}"

    # record_type 화이트리스트
    valid_types = {"A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA", "PTR", "SRV", "CAA", "ANY"}
    if record_type.upper() not in valid_types:
        return f"[error] 지원하지 않는 레코드 타입: {record_type}"

    cmd = ["dig", "+noall", "+answer", domain, record_type.upper()]
    if server:
        valid, reason = validate_hostname(server)
        if not valid:
            return f"[error] 네임서버 {reason}"
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
    """WHOIS 조회. 도메인 또는 IP 대상."""
    # 입력 검증
    valid, reason = validate_domain(target)
    if not valid:
        # IP 주소일 수도 있으므로 hostname 패턴으로도 시도
        valid, reason = validate_hostname(target)
        if not valid:
            return f"[error] {reason}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "whois", target,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
    valid, reason = validate_hostname(host)
    if not valid:
        return f"[error] {reason}"

    count = max(1, min(count, 20))

    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(count), "-W", "2", host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        return stdout.decode() or stderr.decode()
    except asyncio.TimeoutError:
        return "[error] ping timeout"


@mcp.tool()
async def traceroute(host: str, max_hops: int = 20) -> str:
    """traceroute 실행 (UDP 모드)."""
    valid, reason = validate_hostname(host)
    if not valid:
        return f"[error] {reason}"

    max_hops = max(1, min(max_hops, 30))

    try:
        proc = await asyncio.create_subprocess_exec(
            "traceroute", "-m", str(max_hops), "-w", "2", host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        return stdout.decode() or stderr.decode()
    except asyncio.TimeoutError:
        return "[error] traceroute timeout"


@mcp.tool()
async def port_check(host: str, ports: str = "22,80,443") -> str:
    """TCP 포트 연결 테스트."""
    valid, reason = validate_hostname(host)
    if not valid:
        return f"[error] {reason}"

    results = []
    port_list = ports.split(",")

    # 포트 개수 제한
    if len(port_list) > 100:
        return "[error] 최대 100개 포트까지 지원합니다"

    for port_str in port_list:
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
    # Streamable HTTP transport — endpoint: /mcp
    mcp.run(transport="http", host="0.0.0.0", port=8080)
