# plomada-sketchup-mcp

## Notas heredadas de la memoria

> Pasadas desde `~/.claude/projects/-Users-mats-PROYECTOS/memory` el 08/11/2026 (F8 de JARVIS).

### plomada-mac

SketchUp 2026 (26.2) está en la Mac desde 2026-11-02. Plomada (repo público Mats2208/plomada-sketchup-mcp) clonado en
`~/PROYECTOS/plomada-sketchup-mcp` e instalado a mano: `extension/plomada.rb` + `plomada/` copiados a
`~/Library/Application Support/SketchUp 2026/SketchUp/Plugins/`, y MCP `sketchup` (scope user) →
`.venv/bin/plomada-mcp`. El token vive en `~/.local/share/Plomada` (Ruby y Python caen ahí si no hay LOCALAPPDATA). Puerto 7883.

**Why:** `scripts/install.ps1` es solo Windows y el repo fija uv ==0.11.25.

**How to apply:** `uv sync` con `uvx --from uv==0.11.25 uv sync`. Tras actualizar la extensión, volver a copiarla y
reiniciar SketchUp. Ya no falta SketchUp en la Mac: corrige [[comfyui-mac]].
