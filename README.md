# Moodle MCP Server

## Local use

The server uses stdio by default for MCP Inspector and local MCP clients.

```powershell
.\start-moodle-mcp.cmd
```

## Azure Container Apps

The container serves Streamable HTTP MCP at `/mcp` on port `8000`. It requires
`MOODLE_TOKEN` and optionally accepts `MOODLE_URL`.

Build and test the image locally:

```powershell
docker build -t moodle-mcp .
docker run --rm -p 8000:8000 --env-file .env moodle-mcp
```

Deploy from the repository root after authenticating with `az login`:

```powershell
.\deploy-container-app.ps1 `
  -ResourceGroup <resource-group> `
  -Location <azure-region> `
  -MoodleToken <moodle-web-service-token>
```

The script builds the Dockerfile, deploys an externally reachable Azure Container App,
stores the Moodle token as a Container Apps secret, and prints the MCP endpoint.