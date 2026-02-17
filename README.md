# NPM Docker Agent

[![Publish Docker Image](https://github.com/rokelvisar/npm-agent/actions/workflows/publish.yml/badge.svg)](https://github.com/rokelvisar/npm-agent/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An automated bridge between Docker container labels and Nginx Proxy Manager (NPM). This agent monitors the Docker daemon for container lifecycle events and automatically manages proxy hosts in your NPM instance based on container labels.

## Core Features

- **Zero-Touch Configuration**: Manage your reverse proxy directly from your `docker-compose.yml`.
- **Automatic SSL**: Support for automated Let's Encrypt certificates via NPM.
- **Dynamic Port Detection**: Automatically detects mapped host ports or uses internal container IPs.
- **Self-Healing**: Automatically cleans up proxy hosts when containers are removed.
- **Lightweight**: Built on Alpine Linux with minimal footprint.
- **Monitoring Dashboard**: Simple built-in dashboard for status overview.

## Architecture

```mermaid
graph LR
    subgraph "Docker Host"
        A[Application Container] -- Label: npm.proxy.host --> B(Internal/Host Port)
        B -.-> C[NPM Docker Agent]
    end
    C -- API Request --> D[Nginx Proxy Manager]
    D -- Proxy Traffic --> A
```

## Prerequisites

- **Nginx Proxy Manager**: A running instance of [Nginx Proxy Manager](https://nginxproxymanager.com/).
- **Docker Socket Access**: The agent needs read access to `/var/run/docker.sock`.

## Configuration

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `NPM_API_BASE_URL` | Base URL of your NPM instance (e.g., `https://npm.example.com`) | Yes | - |
| `NPM_API_USER` | Admin email for NPM | Yes | - |
| `NPM_API_PASSWORD` | Admin password for NPM | Yes | - |
| `NPM_DEFAULT_LE_EMAIL` | Default email for Let's Encrypt certificates | No | - |
| `NPM_DEFAULT_FORWARD_HOST` | Fallback IP/Host to forward traffic to | No | Container IP |

### Container Labels

To expose a container, add the following labels:

| Label | Description | Default |
|-------|-------------|---------|
| `npm.proxy.host` | Comma-separated domains (e.g., `app.example.com`) | **Required** |
| `npm.proxy.port` | Internal container port to forward to | `80` |
| `npm.proxy.scheme` | Protocol (`http` or `https`) | `http` |
| `npm.proxy.ssl` | Whether to force SSL & request LE cert (`true`/`false`) | `true` |
| `npm.proxy.forward_host` | Override forward host for this specific container | Env default |

## Deployment

### Docker Compose

```yaml
services:
  npm-agent:
    image: ghcr.io/rokelvisar/npm-agent:main
    container_name: npm-agent
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - NPM_API_BASE_URL=http://npm:81
      - NPM_API_USER=admin@example.com
      - NPM_API_PASSWORD=changeme
      - NPM_DEFAULT_LE_EMAIL=admin@example.com
    restart: unless-stopped
```

## Security

- **Socket Access**: Mount the Docker socket as read-only (`:ro`).
- **Non-Root**: The container runs as a non-privileged user.
- **Network**: Ensure the agent can reach both the Docker socket and the NPM API.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
