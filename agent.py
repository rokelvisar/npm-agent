import docker
import requests
import time
import os
import signal
import sys
import logging
import threading
import http.server
from datetime import datetime, timedelta

# Professional Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger("npm-docker-agent")

# Configuration from Environment Variables
NPM_API_BASE_URL = os.getenv("NPM_API_BASE_URL")
NPM_API_USER = os.getenv("NPM_API_USER")
NPM_API_PASSWORD = os.getenv("NPM_API_PASSWORD")
NPM_DEFAULT_LE_EMAIL = os.getenv("NPM_DEFAULT_LE_EMAIL", "")
NPM_DEFAULT_FORWARD_HOST = os.getenv("NPM_DEFAULT_FORWARD_HOST")
DOCKER_HOST = os.getenv("DOCKER_HOST")

# Validate Required Configuration
if not all([NPM_API_BASE_URL, NPM_API_USER, NPM_API_PASSWORD]):
    logger.error("Missing required environment variables: NPM_API_BASE_URL, NPM_API_USER, NPM_API_PASSWORD")
    sys.exit(1)

class NPMSession:
    """Manages authentication and requests to the Nginx Proxy Manager API."""
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.expires_at = None

    def login(self):
        logger.info(f"Authenticating with NPM API at {NPM_API_BASE_URL}...")
        url = f"{NPM_API_BASE_URL}/api/tokens"
        try:
            response = self.session.post(
                url, 
                json={"identity": NPM_API_USER, "secret": NPM_API_PASSWORD}, 
                timeout=10
            )
            if response.status_code >= 400:
                logger.error(f"Authentication failed ({response.status_code}): {response.text}")
                return False
            
            data = response.json()
            self.token = data["token"]
            expires_str = data["expires"].replace('Z', '+00:00')
            self.expires_at = datetime.fromisoformat(expires_str)
            logger.info(f"Authentication successful. Token expires at {self.expires_at}")
            return True
        except Exception as e:
            logger.error(f"Error during authentication: {e}")
            return False

    def ensure_valid_token(self):
        """Ensures the session has a valid, non-expired token."""
        now = datetime.now().astimezone()
        if not self.token or not self.expires_at or now > (self.expires_at - timedelta(minutes=5)):
            return self.login()
        return True

    def request(self, method, path, **kwargs):
        if not self.ensure_valid_token():
            return None
        
        url = f"{NPM_API_BASE_URL}{path}"
        headers = kwargs.get("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        kwargs["headers"] = headers
        
        try:
            response = self.session.request(method, url, **kwargs)
            if response.status_code == 401:
                logger.warning("Token expired unexpectedly, refreshing...")
                if self.login():
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = self.session.request(method, url, **kwargs)
            
            response.raise_for_status()
            return response
        except Exception as e:
            logger.error(f"API Request failed ({method} {path}): {e}")
            return None

# Global Clients
docker_client = docker.DockerClient(base_url=DOCKER_HOST) if DOCKER_HOST else docker.from_env()
npm_session = NPMSession()

def get_existing_proxy_hosts():
    response = npm_session.request("GET", "/api/nginx/proxy-hosts")
    return response.json() if response else []

def create_proxy_host(domains, forward_host, forward_port, scheme="http", ssl=False):
    if isinstance(domains, str):
        domains = [d.strip() for d in domains.split(",") if d.strip()]
        
    payload = {
        "domain_names": domains,
        "forward_scheme": scheme,
        "forward_host": forward_host,
        "forward_port": int(forward_port),
        "access_list_id": 0,
        "certificate_id": 0,
        "ssl_forced": True,
        "caching_enabled": False,
        "allow_websocket_upgrade": True,
        "block_exploits": True,
        "http2_support": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "meta": {
            "managed_by": "npm-docker-agent"
        },
        "advanced_config": "# Managed by NPM Docker Agent\n",
        "locations": []
    }

    if ssl:
        logger.info(f"Provisioning Let's Encrypt SSL for {domains[0]}...")
        payload["certificate_id"] = "new"
        payload["meta"].update({
            "letsencrypt_email": NPM_DEFAULT_LE_EMAIL,
            "letsencrypt_agree": True
        })

    response = npm_session.request("POST", "/api/nginx/proxy-hosts", json=payload)
    if response:
        logger.info(f"✓ Created proxy host: {', '.join(domains)} -> {forward_host}:{forward_port} (SSL: {ssl})")

def delete_proxy_host(host_id):
    response = npm_session.request("DELETE", f"/api/nginx/proxy-hosts/{host_id}")
    if response:
        logger.info(f"✓ Deleted proxy host ID: {host_id}")

def sync_container_state(container):
    labels = container.labels
    domain_label = labels.get("npm.proxy.host")
    if not domain_label:
        return

    domains = [d.strip() for d in domain_label.split(",") if d.strip()]
    primary_domain = domains[0]
    
    internal_port = labels.get("npm.proxy.port", "80")
    scheme = labels.get("npm.proxy.scheme", "http")
    ssl = labels.get("npm.proxy.ssl", "true").lower() == "true"
    
    # Forward host logic: label -> env default -> container network IP (fallback)
    forward_host = labels.get("npm.proxy.forward_host", NPM_DEFAULT_FORWARD_HOST)
    
    # If no forward host provided, try to detect it from the container's network
    if not forward_host:
        networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
        if networks:
            # Pick the first available network IP
            first_net = list(networks.values())[0]
            forward_host = first_net.get('IPAddress')

    if not forward_host:
        logger.error(f"Could not determine forward host for {container.name}. Specify 'npm.proxy.forward_host' label or NPM_DEFAULT_FORWARD_HOST env.")
        return

    # Dynamic Host Port Detection
    final_port = internal_port
    port_key = f"{internal_port}/tcp"
    ports_config = container.attrs.get('NetworkSettings', {}).get('Ports', {})
    if ports_config and port_key in ports_config:
        mappings = ports_config[port_key]
        if mappings and mappings[0].get('HostPort'):
            final_port = mappings[0]['HostPort']
            logger.info(f"Detected host port mapping for {container.name}: {internal_port} -> {final_port}")
    
    logger.info(f"Syncing {container.name} labels ({primary_domain})...")
    
    hosts = get_existing_proxy_hosts()
    existing = next((h for h in hosts if any(d in h["domain_names"] for d in domains)), None)
    
    if existing:
        # Check if update is needed
        existing_host = existing["forward_host"]
        existing_port = int(existing["forward_port"])
        existing_ssl = existing.get("ssl_forced", False)
        is_managed = existing.get("meta", {}).get("managed_by") == "npm-docker-agent"
        existing_domains = sorted(existing.get("domain_names", []))
        requested_domains = sorted(domains)

        needs_update = False
        if existing_host != forward_host or existing_port != int(final_port):
            needs_update = True
        elif existing_ssl != ssl:
            needs_update = True
        elif not is_managed:
            logger.info(f"Found existing unmanaged host for {primary_domain}. Adopting...")
            needs_update = True
        elif existing_domains != requested_domains:
            needs_update = True

        if needs_update:
             logger.info(f"Configuration change detected for {primary_domain}. Updating...")
             delete_proxy_host(existing["id"])
             create_proxy_host(domains, forward_host, final_port, scheme, ssl)
        else:
            logger.debug(f"Host {primary_domain} is up to date.")
    else:
        create_proxy_host(domains, forward_host, final_port, scheme, ssl)

def cleanup_container_proxy(container_name, labels):
    domain = labels.get("npm.proxy.host")
    if not domain:
        return
        
    logger.info(f"Cleaning up proxy for removed container: {container_name} ({domain})")
    hosts = get_existing_proxy_hosts()
    # Find host by domain
    existing = next((h for h in hosts if domain in h["domain_names"]), None)
    if existing and existing.get("meta", {}).get("managed_by") == "npm-docker-agent":
        delete_proxy_host(existing["id"])

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Simple dashboard to view managed hosts."""
    def log_message(self, format, *args):
        return # Silence server logs

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            
            try:
                hosts = get_existing_proxy_hosts()
                managed_hosts = [h for h in hosts if h.get("meta", {}).get("managed_by") == "npm-docker-agent"]
                
                html = self._generate_html(managed_hosts)
                self.wfile.write(html.encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"<h1>Error</h1><pre>{e}</pre>".encode("utf-8"))
        else:
            self.send_error(404)

    def _generate_html(self, hosts):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        total = len(hosts)
        active = sum(1 for h in hosts if h.get("enabled"))
        ssl_count = sum(1 for h in hosts if h.get("ssl_forced") and h.get("certificate_id") and h.get("certificate_id") != 0)

        rows = ""
        for h in hosts:
            domains_list = h.get("domain_names", [])
            primary = domains_list[0] if domains_list else "—"
            extra = f' <span class="badge-extra">+{len(domains_list)-1} more</span>' if len(domains_list) > 1 else ""
            upstream = f"{h.get('forward_scheme', 'http')}://{h.get('forward_host')}:{h.get('forward_port')}"
            ssl_badge = '<span class="badge badge-ssl">SSL</span>' if (h.get("ssl_forced") and h.get("certificate_id") and h.get("certificate_id") != 0) else '<span class="badge badge-nossl">No SSL</span>'
            status_badge = '<span class="badge badge-active">Active</span>' if h.get("enabled") else '<span class="badge badge-disabled">Disabled</span>'
            cert_id = h.get("certificate_id", 0)
            cert_info = f'Cert #{cert_id}' if cert_id and cert_id != 0 else "—"
            rows += f"""
            <tr>
                <td><span class="domain-primary">{primary}</span>{extra}</td>
                <td><code class="upstream">{upstream}</code></td>
                <td>{ssl_badge}</td>
                <td>{cert_info}</td>
                <td>{status_badge}</td>
            </tr>"""

        empty_row = '<tr><td colspan="5" class="empty-row">No managed proxy hosts found.</td></tr>'

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NPM Docker Agent</title>
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

        :root {{
            --bg: #0f1117;
            --surface: #1a1d27;
            --surface2: #22263a;
            --border: #2e3350;
            --accent: #4f8ef7;
            --accent2: #7c5cfc;
            --green: #22c55e;
            --red: #ef4444;
            --yellow: #f59e0b;
            --text: #e2e8f0;
            --muted: #64748b;
            --radius: 12px;
        }}

        body {{
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 32px 24px;
        }}

        .header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 32px;
            flex-wrap: wrap;
            gap: 12px;
        }}

        .header-left {{
            display: flex;
            align-items: center;
            gap: 14px;
        }}

        .logo {{
            width: 44px;
            height: 44px;
            background: linear-gradient(135deg, var(--accent), var(--accent2));
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 22px;
            flex-shrink: 0;
        }}

        .header-title h1 {{
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--text);
            letter-spacing: -0.3px;
        }}

        .header-title p {{
            font-size: 0.8rem;
            color: var(--muted);
            margin-top: 2px;
        }}

        .refresh-btn {{
            background: var(--surface2);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 8px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.85rem;
            transition: background 0.2s;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}

        .refresh-btn:hover {{ background: var(--border); }}

        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }}

        .stat-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .stat-label {{
            font-size: 0.75rem;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .stat-value {{
            font-size: 2rem;
            font-weight: 700;
            line-height: 1;
        }}

        .stat-value.blue {{ color: var(--accent); }}
        .stat-value.green {{ color: var(--green); }}
        .stat-value.purple {{ color: var(--accent2); }}

        .table-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            overflow: hidden;
        }}

        .table-header {{
            padding: 16px 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .table-header h2 {{
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text);
        }}

        .table-header span {{
            font-size: 0.8rem;
            color: var(--muted);
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        thead th {{
            padding: 11px 20px;
            text-align: left;
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            background: var(--surface2);
            border-bottom: 1px solid var(--border);
        }}

        tbody tr {{
            border-bottom: 1px solid var(--border);
            transition: background 0.15s;
        }}

        tbody tr:last-child {{ border-bottom: none; }}
        tbody tr:hover {{ background: var(--surface2); }}

        td {{
            padding: 14px 20px;
            font-size: 0.875rem;
            vertical-align: middle;
        }}

        .domain-primary {{
            font-weight: 500;
            color: var(--text);
        }}

        .badge-extra {{
            font-size: 0.7rem;
            color: var(--muted);
            margin-left: 6px;
        }}

        code.upstream {{
            font-family: 'Fira Code', 'Cascadia Code', monospace;
            font-size: 0.8rem;
            color: var(--accent);
            background: rgba(79, 142, 247, 0.1);
            padding: 3px 8px;
            border-radius: 5px;
        }}

        .badge {{
            display: inline-block;
            padding: 3px 10px;
            border-radius: 20px;
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.3px;
        }}

        .badge-ssl {{ background: rgba(34,197,94,0.15); color: var(--green); }}
        .badge-nossl {{ background: rgba(239,68,68,0.12); color: var(--red); }}
        .badge-active {{ background: rgba(34,197,94,0.15); color: var(--green); }}
        .badge-disabled {{ background: rgba(239,68,68,0.12); color: var(--red); }}

        .empty-row {{
            text-align: center;
            color: var(--muted);
            padding: 48px 20px !important;
            font-size: 0.9rem;
        }}

        .footer {{
            margin-top: 24px;
            text-align: center;
            font-size: 0.75rem;
            color: var(--muted);
        }}

        .footer a {{
            color: var(--accent);
            text-decoration: none;
        }}

        @media (max-width: 600px) {{
            body {{ padding: 16px 12px; }}
            td, thead th {{ padding: 10px 12px; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <div class="logo">🔀</div>
            <div class="header-title">
                <h1>NPM Docker Agent</h1>
                <p>Nginx Proxy Manager · Auto-sync</p>
            </div>
        </div>
        <a href="/" class="refresh-btn">↻ Refresh</a>
    </div>

    <div class="stats">
        <div class="stat-card">
            <span class="stat-label">Managed Hosts</span>
            <span class="stat-value blue">{total}</span>
        </div>
        <div class="stat-card">
            <span class="stat-label">Active</span>
            <span class="stat-value green">{active}</span>
        </div>
        <div class="stat-card">
            <span class="stat-label">SSL Enabled</span>
            <span class="stat-value purple">{ssl_count}</span>
        </div>
    </div>

    <div class="table-card">
        <div class="table-header">
            <h2>Proxy Hosts</h2>
            <span>Last updated: {now}</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Domain</th>
                    <th>Upstream</th>
                    <th>SSL</th>
                    <th>Certificate</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {rows if rows else empty_row}
            </tbody>
        </table>
    </div>

    <div class="footer">
        <a href="https://github.com/rokreativa/npm-agent" target="_blank">npm-docker-agent</a>
        &nbsp;·&nbsp; Monitoring Docker events &amp; syncing with Nginx Proxy Manager
    </div>
</body>
</html>"""

def start_dashboard():
    server_address = ('', 8080)
    httpd = http.server.ThreadingHTTPServer(server_address, DashboardHandler)
    logger.info("Dashboard available at http://localhost:8080")
    httpd.serve_forever()

def main():
    # Signal handling
    def stop_signal(sig, frame):
        logger.info("Shutdown signal received. Exiting...")
        sys.exit(0)

    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    
    logger.info("NPM Docker Agent starting...")
    
    # Docker connection check
    try:
        docker_client.ping()
        logger.info("Connected to Docker daemon.")
    except Exception as e:
        logger.error(f"Cannot connect to Docker daemon: {e}")
        time.sleep(10)
        return

    # Background Dashboard
    threading.Thread(target=start_dashboard, daemon=True).start()

    # Initial Sync
    logger.info("Performing initial container synchronization...")
    for container in docker_client.containers.list():
        try:
            sync_container_state(container)
        except Exception as e:
            logger.error(f"Error syncing container {container.name}: {e}")

    # Event Loop
    logger.info("Monitoring Docker events...")
    for event in docker_client.events(decode=True):
        action = event.get("Action")
        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})
        name = attributes.get("name")

        if action == "start":
            logger.info(f"Container started: {name}")
            try:
                container = docker_client.containers.get(actor["ID"])
                sync_container_state(container)
            except Exception as e:
                logger.error(f"Failed to process start event for {name}: {e}")

        elif action in ["die", "destroy", "stop"]:
            if "npm.proxy.host" in attributes:
                cleanup_container_proxy(name, attributes)

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            logger.critical(f"Unexpected runtime error: {e}. Restarting in 10s...")
            time.sleep(10)
