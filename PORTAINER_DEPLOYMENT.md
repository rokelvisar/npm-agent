# Deploying NPM Docker Agent in Portainer

This guide explains how to deploy the NPM Docker Agent using Portainer to manage Nginx Proxy Manager proxy hosts automatically.

## Prerequisites

- Portainer installed and running
- Nginx Proxy Manager instance accessible
- NPM admin credentials

## Common Permission Issue

When deploying containers that need access to the Docker socket (`/var/run/docker.sock`), you might encounter:

```
PermissionError: [Errno 13] Permission denied
```

This occurs because:
1. The container runs as a non-root user (`agent`) for security
2. The user doesn't have permission to access the Docker socket

## Solution Options

### Option 1: Run Container as Root (Simplest for Portainer)

**In Portainer Stack/Container Settings:**

1. Navigate to **Stacks** → **Add Stack** (or edit existing container)
2. Use the following compose configuration:

```yaml
version: '3.8'

services:
  npm-agent:
    image: ghcr.io/rokelvisar/npm-agent:main
    container_name: npm-agent
    restart: unless-stopped
    user: root  # Run as root to access docker socket
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - NPM_API_BASE_URL=http://npm:81
      - NPM_API_USER=admin@example.com
      - NPM_API_PASSWORD=your-secure-password
      - NPM_DEFAULT_LE_EMAIL=admin@example.com
    networks:
      - npm_network

networks:
  npm_network:
    external: true
```

**For Container Deployment (UI):**
- Go to **Containers** → **Add container**
- Under **Advanced container settings** → **Runtime & Resources**
- Set **User** to `0` or `root`

### Option 2: Match Docker Group GID (More Secure)

If you want to maintain non-root execution:

1. Find your Docker socket group GID on the host:
```bash
stat -c '%g' /var/run/docker.sock
```

2. Use this compose file with the correct GID:

```yaml
version: '3.8'

services:
  npm-agent:
    image: ghcr.io/rokelvisar/npm-agent:main
    container_name: npm-agent
    restart: unless-stopped
    user: "1000:999"  # Replace 999 with your docker GID
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - NPM_API_BASE_URL=http://npm:81
      - NPM_API_USER=admin@example.com
      - NPM_API_PASSWORD=your-secure-password
      - NPM_DEFAULT_LE_EMAIL=admin@example.com
    networks:
      - npm_network

networks:
  npm_network:
    external: true
```

### Option 3: Remote Docker Host (No Socket Needed)

If your Docker daemon exposes a TCP endpoint:

```yaml
version: '3.8'

services:
  npm-agent:
    image: ghcr.io/rokelvisar/npm-agent:main
    container_name: npm-agent
    restart: unless-stopped
    environment:
      - DOCKER_HOST=tcp://your-docker-host-ip:2375
      - NPM_API_BASE_URL=http://npm:81
      - NPM_API_USER=your-npm-email@example.com
      - NPM_API_PASSWORD=your-secure-password
      - NPM_DEFAULT_LE_EMAIL=your-npm-email@example.com
    networks:
      - npm_network

networks:
  npm_network:
    external: true
```

## Complete Portainer Stack Example

Here's a full stack including NPM and the agent:

```yaml
version: '3.8'

services:
  npm:
    image: 'jc21/nginx-proxy-manager:latest'
    container_name: npm
    restart: unless-stopped
    ports:
      - '80:80'
      - '443:443'
      - '81:81'
    volumes:
      - npm_data:/data
      - npm_letsencrypt:/etc/letsencrypt
    networks:
      - npm_network

  npm-agent:
    image: ghcr.io/rokelvisar/npm-agent:main
    container_name: npm-agent
    restart: unless-stopped
    user: root  # Required for docker socket access
    depends_on:
      - npm
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - NPM_API_BASE_URL=http://npm:81
      - NPM_API_USER=admin@example.com
      - NPM_API_PASSWORD=your-secure-password
      - NPM_DEFAULT_LE_EMAIL=admin@example.com
    networks:
      - npm_network

  # Example application to be proxied
  whoami:
    image: traefik/whoami
    container_name: whoami
    restart: unless-stopped
    labels:
      - "npm.proxy.host=whoami.example.com"
      - "npm.proxy.port=80"
      - "npm.proxy.ssl=true"
    networks:
      - npm_network

volumes:
  npm_data:
  npm_letsencrypt:

networks:
  npm_network:
    driver: bridge
```

## Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `NPM_API_BASE_URL` | Yes | NPM API endpoint | `http://npm:81` or `http://npm.example.com` |
| `NPM_API_USER` | Yes | NPM login email | `your-npm-email@example.com` |
| `NPM_API_PASSWORD` | Yes | NPM password | `your-secure-password` |
| `NPM_DEFAULT_LE_EMAIL` | No | Let's Encrypt email | `your-npm-email@example.com` |
| `DOCKER_HOST` | No | Remote Docker endpoint | `tcp://your-docker-host-ip:2375` |

## Container Labels

Add these labels to any container you want to proxy:

```yaml
labels:
  - "npm.proxy.host=example.com,www.example.com"  # Comma-separated domains
  - "npm.proxy.port=80"                            # Container port
  - "npm.proxy.ssl=true"                           # Enable SSL with Let's Encrypt
  - "npm.proxy.scheme=http"                        # http or https (optional)
  - "npm.proxy.forward_host=custom-host"           # Override target host (optional)
```

## Troubleshooting

### 1. Permission Denied Error
- **Solution**: Use `user: root` in the compose file or set user to `0` in Portainer UI

### 2. Cannot Connect to Docker Daemon
- **Verify socket exists**: `ls -l /var/run/docker.sock` on the host
- **Check volume mount**: Ensure `/var/run/docker.sock:/var/run/docker.sock:ro` is present

### 3. NPM Authentication Failed
- **Verify credentials**: Test login at `http://npm.example.com:81`
- **Check network**: Ensure agent can reach NPM (use same network)

### 4. SSL Certificate Not Generated
- **Verify email**: Set `NPM_DEFAULT_LE_EMAIL` environment variable
- **Check DNS**: Ensure domain resolves to your server before cert request

## Viewing the Dashboard

The agent exposes a dashboard at `http://localhost:8080` (or the container IP in Portainer).

To access it:
1. In Portainer, go to your `npm-agent` container
2. Click on **Publish a new network port**
3. Map host port `8080` to container port `8080`
4. Access at `http://your-host:8080`

## GitLab CI/CD Integration

To deploy via GitLab Runner with Portainer:

```yaml
deploy:
  stage: deploy
  script:
    - docker stack deploy -c docker-compose.yml npm-stack
  only:
    - main
```

Or use Portainer's Webhook feature:
1. In Portainer, go to your stack
2. Copy the webhook URL
3. Add to GitLab: **Settings** → **Webhooks** → Paste URL
