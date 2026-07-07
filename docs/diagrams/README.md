# Diagram sources

Editable Mermaid sources for the diagrams in [`../ARCHITECTURE.md`](../ARCHITECTURE.md).
Rendered PNG/SVG live in [`../images/`](../images/).

| Source | Renders to |
|---|---|
| `local-architecture.mmd` | `../images/local-architecture.{png,svg}` |
| `aws-target-architecture.mmd` | `../images/aws-target-architecture.{png,svg}` |

## Re-render (after editing a `.mmd`)

No host install needed — render with Docker (works on Apple Silicon; uses system Chromium
because the prebuilt `mermaid-cli` image is amd64-only):

```bash
# from agentcore-dual-gateway-poc/
docker run --rm --platform linux/arm64 -v "$PWD/docs:/data" node:20-bookworm bash -c '
  export PUPPETEER_SKIP_DOWNLOAD=true
  apt-get update -qq && apt-get install -y -qq chromium fonts-noto-color-emoji fonts-dejavu >/dev/null 2>&1
  npm i -g @mermaid-js/mermaid-cli >/dev/null 2>&1
  echo "{\"executablePath\":\"/usr/bin/chromium\",\"args\":[\"--no-sandbox\"]}" > /tmp/pp.json
  for d in local-architecture aws-target-architecture; do
    mmdc -i /data/diagrams/$d.mmd -o /data/images/$d.svg -p /tmp/pp.json -b white
    mmdc -i /data/diagrams/$d.mmd -o /data/images/$d.png -p /tmp/pp.json -b white -s 2
  done
'
```

On an amd64 host, drop `--platform linux/arm64` (or just use `minlag/mermaid-cli`).
GitHub and VS Code also render the ```mermaid``` blocks in `ARCHITECTURE.md` directly.
