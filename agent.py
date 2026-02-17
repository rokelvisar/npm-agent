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
docker_client = docker.from_env()
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
        # Professional UI simplified
        rows = ""
        for h in hosts:
            domains = ", ".join(h.get("domain_names", []))
            upstream = f"{h.get('forward_host')}:{h.get('forward_port')}"
            ssl = "✅" if h.get("ssl_forced") else "❌"
            status = "🟢 Active" if h.get("enabled") else "🔴 Disabled"
            rows += f"<tr><td>{domains}</td><td>{upstream}</td><td>{ssl}</td><td>{status}</td></tr>"

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>NPM Docker Agent</title>
            <style>
                body {{ font-family: sans-serif; padding: 40px; background: #f4f7f6; }}
                .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                th, td {{ text-align: left; padding: 12px; border-bottom: 1px solid #eee; }}
                th {{ background: #fafafa; }}
                h1 {{ margin-top: 0; color: #333; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>NPM Docker Agent</h1>
                <p>Monitoring Docker events and synchronizing with Nginx Proxy Manager.</p>
                <table>
                    <thead><tr><th>Domains</th><th>Upstream</th><th>SSL</th><th>Status</th></tr></thead>
                    <tbody>{rows if rows else '<tr><td colspan="4">No managed hosts found.</td></tr>'}</tbody>
                </table>
            </div>
        </body>
        </html>
        """

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
