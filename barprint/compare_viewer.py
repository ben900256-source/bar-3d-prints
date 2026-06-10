from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template
import html
import json
import os

@dataclass(frozen=True)
class CompareViewerPaths:
    default_stl: Path
    game_glb: Path
    post_thickening_stl: Path
    viewer_html: Path

    @property
    def print_stl(self) -> Path:
        return self.default_stl


@dataclass(frozen=True)
class DebugStagePaths:
    debug_dir: Path
    stage_report: Path
    viewer_html: Path
    game_glb: Path
    post_thickening_stl: Path
    opaque_print_source_glb: Path


@dataclass(frozen=True)
class CompareViewerRow:
    label: str
    default_stl: Path
    game_glb: Path
    post_thickening_stl: Path
    opaque_print_source_glb: Path | None = None


def compare_viewer_paths(out_path: Path) -> CompareViewerPaths:
    base_stem = _base_stem(out_path)
    return CompareViewerPaths(
        default_stl=out_path,
        game_glb=out_path.with_name(f"{base_stem}_game.glb"),
        post_thickening_stl=out_path.with_name(f"{base_stem}_post{out_path.suffix}"),
        viewer_html=out_path.with_name(f"{base_stem}_viewer.html"),
    )


def debug_stage_paths(out_path: Path, debug_dir: Path | None = None) -> DebugStagePaths:
    base_stem = _base_stem(out_path)
    if debug_dir is None:
        debug_dir = out_path.with_name(f"{base_stem}_debug")
    return DebugStagePaths(
        debug_dir=debug_dir,
        stage_report=debug_dir / "stage_report.json",
        viewer_html=debug_dir / f"{base_stem}_debug_viewer.html",
        game_glb=debug_dir / f"{base_stem}_game.glb",
        post_thickening_stl=debug_dir / f"{base_stem}_post.stl",
        opaque_print_source_glb=debug_dir / f"{base_stem}_opaque_print_source.glb",
    )


def write_compare_viewer_html(
    viewer_html: Path,
    *,
    game_glb: Path,
    post_thickening_stl: Path,
    default_stl: Path | None = None,
    print_stl: Path | None = None,
    opaque_print_source_glb: Path | None = None,
) -> None:
    if default_stl is None:
        default_stl = print_stl
    if default_stl is None:
        raise ValueError("default_stl is required")
    write_compare_viewer_rows_html(
        viewer_html,
        rows=[
            CompareViewerRow(
                label="",
                default_stl=default_stl,
                game_glb=game_glb,
                post_thickening_stl=post_thickening_stl,
                opaque_print_source_glb=opaque_print_source_glb,
            )
        ],
    )


def write_compare_viewer_rows_html(viewer_html: Path, *, rows: list[CompareViewerRow]) -> None:
    payload = _viewer_rows_payload(viewer_html=viewer_html, rows=rows)
    html = VIEWER_TEMPLATE.substitute(
        assets_json=json.dumps(payload["assets"], sort_keys=True),
        pane_count=payload["pane_count"],
        pane_specs_json=json.dumps(payload["pane_specs"], sort_keys=True),
        row_count=payload["row_count"],
        rows_html=payload["rows_html"],
        viewer_runtime_html=VIEWER_RUNTIME_TEMPLATE.substitute(
            assets_json=json.dumps(payload["assets"], sort_keys=True),
            pane_specs_json=json.dumps(payload["pane_specs"], sort_keys=True),
        ),
    )
    viewer_html.parent.mkdir(parents=True, exist_ok=True)
    viewer_html.write_text(html, encoding="utf-8")


def _viewer_rows_payload(*, viewer_html: Path, rows: list[CompareViewerRow]) -> dict:
    if not rows:
        raise ValueError("at least one viewer row is required")
    assets = {}
    pane_specs = []
    rows_html = []
    pane_count = 0
    legacy_single_keys = len(rows) == 1
    legacy_asset_keys = {
        "default": "defaultStl",
        "game": "gameGlb",
        "post": "postThickeningStl",
        "opaque": "opaquePrintSourceGlb",
    }
    for row_index, row in enumerate(rows):
        row_id = f"row{row_index}"
        row_pane_specs = [
            ("default", "Default", "stl", row.default_stl, "defaultPrint"),
            ("game", "In-Game", "glb", row.game_glb, None),
            ("post", "Post-Thickening", "stl", row.post_thickening_stl, "postThickening"),
        ]
        if row.opaque_print_source_glb is not None:
            row_pane_specs.append(
                ("opaque", "Opaque Print Source", "glb", row.opaque_print_source_glb, "opaquePrintSource")
            )
        pane_count = max(pane_count, len(row_pane_specs))
        panes_html = []
        for pane_id, label, kind, asset_path, material in row_pane_specs:
            unique_id = f"{row_id}_{pane_id}"
            asset_key = legacy_asset_keys[pane_id] if legacy_single_keys else f"{unique_id}Asset"
            assets[asset_key] = _relative_asset_path(viewer_html, asset_path)
            spec = {"id": unique_id, "kind": kind, "assetKey": asset_key}
            if material is not None:
                spec["material"] = material
            pane_specs.append(spec)
            panes_html.append(_pane_html(unique_id, label))
        rows_html.append(_row_html(row_id, row.label, panes_html))
    return {
        "assets": assets,
        "pane_specs": pane_specs,
        "rows_html": "\n      ".join(rows_html),
        "pane_count": pane_count,
        "row_count": len(rows),
        "title": "BAR Print Compare Viewer",
    }


def _base_stem(path: Path) -> str:
    stem = path.stem
    for suffix in ("_pre", "_default"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _relative_asset_path(viewer_html: Path, asset_path: Path) -> str:
    return Path(os.path.relpath(asset_path, viewer_html.parent)).as_posix()


def _pane_html(pane_id: str, label: str) -> str:
    return (
        f'<section class="pane" data-pane="{html.escape(pane_id)}"><div class="label">{html.escape(label)}</div>'
        '<div class="viewport"></div><div class="status">Loading</div></section>'
    )


def _row_html(row_id: str, label: str, panes_html: list[str]) -> str:
    row_class = "pose-row" if label else "pose-row no-row-label"
    return (
        f'<section class="{row_class}" data-row="{html.escape(row_id)}">'
        f'<div class="row-heading">{html.escape(label)}</div>'
        f'<div class="row-panes">{"".join(panes_html)}</div>'
        "</section>"
    )


VIEWER_TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BAR Print Compare Viewer</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #151515;
      color: #f3f0e9;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      overflow: hidden;
      background: #151515;
    }
    .app {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 100vh;
    }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid #3b3933;
      background: #24231f;
    }
    .title {
      min-width: 0;
      font-size: 14px;
      font-weight: 700;
      color: #f7f1df;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }
    button,
    label.toggle {
      min-height: 32px;
      border: 1px solid #5a564c;
      border-radius: 6px;
      background: #302f2a;
      color: #f7f1df;
      font: inherit;
      font-size: 13px;
    }
    button {
      padding: 0 12px;
      cursor: pointer;
    }
    button:hover,
    label.toggle:hover {
      border-color: #c28a3a;
      background: #39352d;
    }
    label.toggle {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 0 10px;
      user-select: none;
    }
    .grid {
      display: grid;
      grid-auto-rows: minmax(340px, 1fr);
      min-height: 0;
      overflow: auto;
    }
    .pose-row {
      display: grid;
      grid-template-columns: minmax(108px, 132px) minmax(0, 1fr);
      min-height: 0;
      border-bottom: 1px solid #36342f;
    }
    .pose-row:last-child {
      border-bottom: 0;
    }
    .pose-row.no-row-label {
      grid-template-columns: 1fr;
    }
    .pose-row.no-row-label .row-heading {
      display: none;
    }
    .row-heading {
      display: flex;
      align-items: center;
      justify-content: center;
      min-width: 0;
      padding: 12px 10px;
      border-right: 1px solid #36342f;
      background: #201f1c;
      color: #f7f1df;
      font-size: 13px;
      font-weight: 800;
      overflow-wrap: anywhere;
      text-align: center;
    }
    .row-panes {
      display: grid;
      grid-template-columns: repeat($pane_count, minmax(0, 1fr));
      min-width: 0;
      min-height: 0;
    }
    .pane {
      position: relative;
      min-width: 0;
      min-height: 0;
      border-right: 1px solid #36342f;
      background: #191917;
    }
    .row-panes .pane:last-child {
      border-right: 0;
    }
    .label {
      position: absolute;
      z-index: 2;
      top: 10px;
      left: 10px;
      max-width: calc(100% - 20px);
      padding: 5px 8px;
      border-radius: 6px;
      background: rgba(28, 27, 24, 0.84);
      border: 1px solid rgba(208, 194, 162, 0.24);
      color: #f7f1df;
      font-size: 12px;
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .viewport {
      position: absolute;
      inset: 0;
    }
    canvas {
      display: block;
      width: 100%;
      height: 100%;
    }
    .status {
      position: absolute;
      z-index: 2;
      left: 10px;
      right: 10px;
      bottom: 10px;
      max-width: none;
      width: auto;
      padding: 5px 8px;
      border-radius: 6px;
      background: rgba(28, 27, 24, 0.82);
      border: 1px solid rgba(208, 194, 162, 0.2);
      color: #d8d1c2;
      font-size: 12px;
      overflow-wrap: anywhere;
      white-space: normal;
      word-break: break-word;
    }
    .status.error {
      background: rgba(58, 28, 22, 0.88);
      border-color: rgba(239, 128, 86, 0.5);
      color: #ffd9c7;
    }
    @media (max-width: 760px) {
      body {
        overflow: auto;
      }
      .app {
        min-height: 100dvh;
      }
      .toolbar {
        align-items: flex-start;
        flex-direction: column;
      }
      .grid {
        grid-auto-rows: auto;
      }
      .pose-row,
      .pose-row.no-row-label {
        grid-template-columns: 1fr;
      }
      .row-heading {
        justify-content: flex-start;
        min-height: 38px;
        border-right: 0;
        border-bottom: 1px solid #36342f;
      }
      .row-panes {
        grid-template-columns: 1fr;
        grid-auto-rows: minmax(280px, 45vh);
      }
      .pane,
      .pane:last-child {
        border-right: 0;
        border-bottom: 1px solid #36342f;
      }
      .status {
        word-break: break-all;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="toolbar">
      <div class="title">BAR Print Compare Viewer</div>
      <div class="actions">
        <button id="resetView" type="button">Reset View</button>
        <label class="toggle"><input id="wireframe" type="checkbox"> Wireframe</label>
      </div>
    </header>
    <main class="grid" data-row-count="$row_count">
      $rows_html
    </main>
  </div>
  $viewer_runtime_html
</body>
</html>
"""
)


VIEWER_RUNTIME_TEMPLATE = Template(
    """<script>
    window.__barprintViewerStarted = false;
    window.setTimeout(() => {
      if (window.__barprintViewerStarted) {
        return;
      }
      const message = "Embedded viewer script did not start.";
      document.querySelectorAll(".status").forEach((status) => {
        if (status.textContent.trim() === "Loading") {
          status.textContent = message;
          status.classList.add("error");
        }
      });
    }, 8000);
  </script>
  <script>
    window.__barprintViewerStarted = true;

    const assets = $assets_json;
    const paneSpecs = $pane_specs_json;
    const LOAD_TIMEOUT_MS = 60000;
    const FOV_DEG = 35;
    const DEFAULT_COLORS = {
      defaultPrint: [0.847, 0.816, 0.749],
      postThickening: [0.839, 0.651, 0.290],
      opaquePrintSource: [0.847, 0.816, 0.749],
      game: [0.780, 0.760, 0.690]
    };
    const views = [];
    const loadedBoxes = [];
    let fitBox = null;
    let syncing = false;
    let wireframeEnabled = false;

    for (const spec of paneSpecs) {
      views.push(createView(spec.id));
    }

    const paneLoads = paneSpecs.map((spec) => {
      const view = viewById(spec.id);
      const url = assets[spec.assetKey];
      const materialColor = spec.material ? DEFAULT_COLORS[spec.material] : null;
      return loadPane(view, url, () => (
        spec.kind === "stl"
          ? loadStlPane(view, url, materialColor || DEFAULT_COLORS.defaultPrint)
          : loadGlbPane(view, url, materialColor)
      ));
    });

    Promise.allSettled(paneLoads).then(() => {
      if (loadedBoxes.length === 0) {
        for (const view of views) {
          if (view.pane.querySelector(".status").textContent.trim() === "Loading") {
            setStatus(view, "No model assets loaded.", "error");
          }
        }
      }
    });

    document.getElementById("resetView").addEventListener("click", resetView);
    document.getElementById("wireframe").addEventListener("change", (event) => {
      setWireframe(event.target.checked);
    });
    window.addEventListener("resize", resizeAll);
    resizeAll();
    render();

    function createView(id) {
      const pane = document.querySelector('[data-pane="' + id + '"]');
      const viewport = pane.querySelector(".viewport");
      const canvas = document.createElement("canvas");
      viewport.appendChild(canvas);
      const gl = canvas.getContext("webgl", { antialias: true });
      if (!gl) {
        throw new Error("WebGL is not available in this browser.");
      }
      const program = createProgram(gl);
      gl.useProgram(program.program);
      gl.enable(gl.DEPTH_TEST);
      gl.enable(gl.CULL_FACE);
      gl.cullFace(gl.BACK);
      gl.clearColor(0.098, 0.098, 0.090, 1);

      const view = {
        id,
        pane,
        viewport,
        canvas,
        gl,
        program,
        meshes: [],
        orbit: {
          target: [0, 0, 0],
          distance: 100,
          yaw: -0.92,
          pitch: 0.50,
          near: 0.01,
          far: 1200
        },
        drag: null
      };
      installControls(view);
      if (window.ResizeObserver) {
        const observer = new ResizeObserver(() => resizeView(view));
        observer.observe(viewport);
      }
      return view;
    }

    function createProgram(gl) {
      const vertexSource = [
        "attribute vec3 aPosition;",
        "attribute vec3 aNormal;",
        "uniform mat4 uViewProj;",
        "varying vec3 vNormal;",
        "void main() {",
        "  vNormal = normalize(aNormal);",
        "  gl_Position = uViewProj * vec4(aPosition, 1.0);",
        "}"
      ].join("\\n");
      const fragmentSource = [
        "precision mediump float;",
        "uniform vec3 uColor;",
        "varying vec3 vNormal;",
        "void main() {",
        "  vec3 normal = normalize(vNormal);",
        "  vec3 key = normalize(vec3(0.46, -0.58, 0.68));",
        "  vec3 fill = normalize(vec3(-0.60, 0.36, 0.48));",
        "  float top = clamp(normal.z * 0.5 + 0.5, 0.0, 1.0);",
        "  float light = 0.28 + 0.25 * top + 0.62 * max(dot(normal, key), 0.0) + 0.18 * max(dot(normal, fill), 0.0);",
        "  vec3 color = uColor * light + vec3(0.035, 0.032, 0.026);",
        "  gl_FragColor = vec4(color, 1.0);",
        "}"
      ].join("\\n");
      const vertex = compileShader(gl, gl.VERTEX_SHADER, vertexSource);
      const fragment = compileShader(gl, gl.FRAGMENT_SHADER, fragmentSource);
      const program = gl.createProgram();
      gl.attachShader(program, vertex);
      gl.attachShader(program, fragment);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(program) || "Could not link WebGL program.");
      }
      return {
        program,
        aPosition: gl.getAttribLocation(program, "aPosition"),
        aNormal: gl.getAttribLocation(program, "aNormal"),
        uViewProj: gl.getUniformLocation(program, "uViewProj"),
        uColor: gl.getUniformLocation(program, "uColor")
      };
    }

    function compileShader(gl, type, source) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(gl.getShaderInfoLog(shader) || "Could not compile WebGL shader.");
      }
      return shader;
    }

    function installControls(view) {
      view.canvas.addEventListener("pointerdown", (event) => {
        view.canvas.setPointerCapture(event.pointerId);
        view.drag = {
          x: event.clientX,
          y: event.clientY,
          button: event.button,
          pan: event.shiftKey || event.button === 1 || event.button === 2
        };
      });
      view.canvas.addEventListener("pointermove", (event) => {
        if (!view.drag) {
          return;
        }
        const dx = event.clientX - view.drag.x;
        const dy = event.clientY - view.drag.y;
        view.drag.x = event.clientX;
        view.drag.y = event.clientY;
        if (view.drag.pan) {
          panView(view, dx, dy);
        } else {
          view.orbit.yaw -= dx * 0.007;
          view.orbit.pitch = clamp(view.orbit.pitch + dy * 0.007, -1.35, 1.35);
        }
        syncFrom(view);
      });
      view.canvas.addEventListener("pointerup", () => {
        view.drag = null;
      });
      view.canvas.addEventListener("pointercancel", () => {
        view.drag = null;
      });
      view.canvas.addEventListener("contextmenu", (event) => {
        event.preventDefault();
      });
      view.canvas.addEventListener("wheel", (event) => {
        event.preventDefault();
        const factor = Math.exp(event.deltaY * 0.001);
        view.orbit.distance = clamp(view.orbit.distance * factor, 0.01, 1000000);
        syncFrom(view);
      }, { passive: false });
    }

    function panView(view, dx, dy) {
      const camera = cameraVectors(view.orbit);
      const scale = view.orbit.distance * Math.tan(degToRad(FOV_DEG) / 2) * 2 / Math.max(1, view.canvas.height);
      const move = vec3Add(vec3Scale(camera.right, -dx * scale), vec3Scale(camera.up, dy * scale));
      view.orbit.target = vec3Add(view.orbit.target, move);
    }

    function viewById(id) {
      return views.find((view) => view.id === id);
    }

    async function loadPane(view, url, loadAsset) {
      setStatus(view, "Loading " + url);
      try {
        const box = await withTimeout(loadAsset());
        if (boxIsEmpty(box)) {
          throw new Error("loaded asset has no visible geometry");
        }
        registerLoadedBox(box);
        setStatus(view, "");
        return box;
      } catch (error) {
        console.error("Failed to load " + url, error);
        setStatus(view, assetErrorMessage(url, error), "error");
        throw error;
      }
    }

    function withTimeout(promise) {
      let timeoutId = null;
      const timeout = new Promise((resolve, reject) => {
        timeoutId = window.setTimeout(() => {
          reject(new Error("timed out after " + Math.round(LOAD_TIMEOUT_MS / 1000) + "s"));
        }, LOAD_TIMEOUT_MS);
      });
      return Promise.race([promise, timeout]).finally(() => {
        if (timeoutId !== null) {
          window.clearTimeout(timeoutId);
        }
      });
    }

    function registerLoadedBox(box) {
      loadedBoxes.push(boxClone(box));
      fitBox = boxClone(loadedBoxes[0]);
      for (let index = 1; index < loadedBoxes.length; index += 1) {
        fitBox = boxUnion(fitBox, loadedBoxes[index]);
      }
      resetView();
    }

    async function fetchArrayBuffer(url) {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error("HTTP " + response.status + " " + response.statusText);
      }
      return await response.arrayBuffer();
    }

    async function loadStlPane(view, url, color) {
      const buffer = await fetchArrayBuffer(url);
      const mesh = parseStl(buffer, color);
      addMesh(view, mesh);
      return mesh.box;
    }

    async function loadGlbPane(view, url, overrideColor) {
      const buffer = await fetchArrayBuffer(url);
      const meshes = parseGlb(buffer, overrideColor);
      let box = boxEmpty();
      for (const mesh of meshes) {
        addMesh(view, mesh);
        box = boxUnion(box, mesh.box);
      }
      return box;
    }

    function addMesh(view, mesh) {
      const gl = view.gl;
      mesh.positionBuffer = createArrayBuffer(gl, mesh.positions);
      mesh.normalBuffer = createArrayBuffer(gl, mesh.normals);
      mesh.lineBuffer = createArrayBuffer(gl, mesh.linePositions);
      mesh.triangleCount = mesh.positions.length / 3;
      mesh.lineCount = mesh.linePositions.length / 3;
      view.meshes.push(mesh);
    }

    function createArrayBuffer(gl, data) {
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      return buffer;
    }

    function parseStl(buffer, color) {
      const binary = looksLikeBinaryStl(buffer);
      return binary ? parseBinaryStl(buffer, color) : parseAsciiStl(buffer, color);
    }

    function looksLikeBinaryStl(buffer) {
      if (buffer.byteLength < 84) {
        return false;
      }
      const view = new DataView(buffer);
      const count = view.getUint32(80, true);
      return 84 + count * 50 === buffer.byteLength;
    }

    function parseBinaryStl(buffer, color) {
      const view = new DataView(buffer);
      const count = view.getUint32(80, true);
      const positions = new Float32Array(count * 9);
      const normals = new Float32Array(count * 9);
      let offset = 84;
      for (let tri = 0; tri < count; tri += 1) {
        const normal = [view.getFloat32(offset, true), view.getFloat32(offset + 4, true), view.getFloat32(offset + 8, true)];
        offset += 12;
        const vertices = [];
        for (let vertex = 0; vertex < 3; vertex += 1) {
          vertices.push([view.getFloat32(offset, true), view.getFloat32(offset + 4, true), view.getFloat32(offset + 8, true)]);
          offset += 12;
        }
        offset += 2;
        const finalNormal = vec3Length(normal) > 0.000001 ? vec3Normalize(normal) : faceNormal(vertices[0], vertices[1], vertices[2]);
        for (let vertex = 0; vertex < 3; vertex += 1) {
          writeVec3(positions, tri * 9 + vertex * 3, vertices[vertex]);
          writeVec3(normals, tri * 9 + vertex * 3, finalNormal);
        }
      }
      return meshFromFlat(positions, normals, color);
    }

    function parseAsciiStl(buffer, color) {
      const text = new TextDecoder("utf-8").decode(buffer);
      const matches = text.matchAll(/vertex\\s+([-+0-9.eE]+)\\s+([-+0-9.eE]+)\\s+([-+0-9.eE]+)/g);
      const vertices = [];
      for (const match of matches) {
        vertices.push([Number(match[1]), Number(match[2]), Number(match[3])]);
      }
      const triCount = Math.floor(vertices.length / 3);
      const positions = new Float32Array(triCount * 9);
      const normals = new Float32Array(triCount * 9);
      for (let tri = 0; tri < triCount; tri += 1) {
        const a = vertices[tri * 3];
        const b = vertices[tri * 3 + 1];
        const c = vertices[tri * 3 + 2];
        const normal = faceNormal(a, b, c);
        writeVec3(positions, tri * 9, a);
        writeVec3(positions, tri * 9 + 3, b);
        writeVec3(positions, tri * 9 + 6, c);
        writeVec3(normals, tri * 9, normal);
        writeVec3(normals, tri * 9 + 3, normal);
        writeVec3(normals, tri * 9 + 6, normal);
      }
      return meshFromFlat(positions, normals, color);
    }

    function parseGlb(buffer, overrideColor) {
      const view = new DataView(buffer);
      if (view.getUint32(0, true) !== 0x46546c67) {
        throw new Error("not a GLB file");
      }
      const version = view.getUint32(4, true);
      if (version !== 2) {
        throw new Error("unsupported GLB version " + version);
      }
      let offset = 12;
      let json = null;
      let bin = null;
      while (offset + 8 <= buffer.byteLength) {
        const chunkLength = view.getUint32(offset, true);
        const chunkType = view.getUint32(offset + 4, true);
        offset += 8;
        const chunk = buffer.slice(offset, offset + chunkLength);
        offset += chunkLength;
        if (chunkType === 0x4e4f534a) {
          json = JSON.parse(new TextDecoder("utf-8").decode(chunk));
        } else if (chunkType === 0x004e4942) {
          bin = chunk;
        }
      }
      if (!json || !bin) {
        throw new Error("GLB missing JSON or binary chunk");
      }
      const meshes = [];
      const scene = json.scenes && json.scenes[json.scene || 0];
      const nodeIndices = scene && scene.nodes ? scene.nodes : (json.nodes || []).map((node, index) => index);
      for (const nodeIndex of nodeIndices) {
        visitGlbNode(json, bin, nodeIndex, mat4Identity(), overrideColor, meshes);
      }
      return meshes;
    }

    function visitGlbNode(json, bin, nodeIndex, parentMatrix, overrideColor, meshes) {
      const node = json.nodes[nodeIndex];
      const local = glbNodeMatrix(node);
      const world = mat4Multiply(parentMatrix, local);
      if (node.mesh !== undefined) {
        const meshDef = json.meshes[node.mesh];
        for (const primitive of meshDef.primitives || []) {
          const mode = primitive.mode === undefined ? 4 : primitive.mode;
          if (mode !== 4 || primitive.attributes.POSITION === undefined) {
            continue;
          }
          meshes.push(glbPrimitiveToMesh(json, bin, primitive, world, overrideColor));
        }
      }
      for (const childIndex of node.children || []) {
        visitGlbNode(json, bin, childIndex, world, overrideColor, meshes);
      }
    }

    function glbPrimitiveToMesh(json, bin, primitive, matrix, overrideColor) {
      const positions = readAccessor(json, bin, primitive.attributes.POSITION);
      const normalAccessor = primitive.attributes.NORMAL;
      const normals = normalAccessor === undefined ? null : readAccessor(json, bin, normalAccessor);
      const indices = primitive.indices === undefined ? null : readAccessor(json, bin, primitive.indices).map((item) => item[0]);
      const vertexCount = indices ? indices.length : positions.length;
      const triCount = Math.floor(vertexCount / 3);
      const flatPositions = new Float32Array(triCount * 9);
      const flatNormals = new Float32Array(triCount * 9);
      const color = overrideColor || glbMaterialColor(json, primitive.material);
      for (let tri = 0; tri < triCount; tri += 1) {
        const triPositions = [];
        const triNormals = [];
        for (let corner = 0; corner < 3; corner += 1) {
          const sourceIndex = indices ? indices[tri * 3 + corner] : tri * 3 + corner;
          const position = transformPoint(matrix, positions[sourceIndex]);
          triPositions.push(position);
          if (normals) {
            triNormals.push(vec3Normalize(transformDirection(matrix, normals[sourceIndex])));
          }
        }
        const computedNormal = faceNormal(triPositions[0], triPositions[1], triPositions[2]);
        for (let corner = 0; corner < 3; corner += 1) {
          const dest = tri * 9 + corner * 3;
          writeVec3(flatPositions, dest, triPositions[corner]);
          writeVec3(flatNormals, dest, normals ? triNormals[corner] : computedNormal);
        }
      }
      return meshFromFlat(flatPositions, flatNormals, color);
    }

    function glbMaterialColor(json, materialIndex) {
      if (materialIndex === undefined || !json.materials || !json.materials[materialIndex]) {
        return DEFAULT_COLORS.game;
      }
      const material = json.materials[materialIndex];
      const pbr = material.pbrMetallicRoughness || {};
      const factor = pbr.baseColorFactor || DEFAULT_COLORS.game;
      return [factor[0], factor[1], factor[2]];
    }

    function readAccessor(json, bin, accessorIndex) {
      const accessor = json.accessors[accessorIndex];
      const viewDef = json.bufferViews[accessor.bufferView];
      const componentCount = accessorTypeSize(accessor.type);
      const componentSize = componentByteSize(accessor.componentType);
      const byteStride = viewDef.byteStride || componentCount * componentSize;
      const baseOffset = (viewDef.byteOffset || 0) + (accessor.byteOffset || 0);
      const dataView = new DataView(bin, baseOffset, viewDef.byteLength - (accessor.byteOffset || 0));
      const result = [];
      for (let item = 0; item < accessor.count; item += 1) {
        const values = [];
        const itemOffset = item * byteStride;
        for (let component = 0; component < componentCount; component += 1) {
          values.push(readComponent(dataView, itemOffset + component * componentSize, accessor.componentType, accessor.normalized));
        }
        result.push(values);
      }
      return result;
    }

    function readComponent(view, offset, componentType, normalized) {
      let value = 0;
      if (componentType === 5120) {
        value = view.getInt8(offset);
        return normalized ? Math.max(value / 127, -1) : value;
      }
      if (componentType === 5121) {
        value = view.getUint8(offset);
        return normalized ? value / 255 : value;
      }
      if (componentType === 5122) {
        value = view.getInt16(offset, true);
        return normalized ? Math.max(value / 32767, -1) : value;
      }
      if (componentType === 5123) {
        value = view.getUint16(offset, true);
        return normalized ? value / 65535 : value;
      }
      if (componentType === 5125) {
        return view.getUint32(offset, true);
      }
      if (componentType === 5126) {
        return view.getFloat32(offset, true);
      }
      throw new Error("unsupported GLB component type " + componentType);
    }

    function accessorTypeSize(type) {
      if (type === "SCALAR") {
        return 1;
      }
      if (type === "VEC2") {
        return 2;
      }
      if (type === "VEC3") {
        return 3;
      }
      if (type === "VEC4") {
        return 4;
      }
      if (type === "MAT4") {
        return 16;
      }
      throw new Error("unsupported GLB accessor type " + type);
    }

    function componentByteSize(componentType) {
      if (componentType === 5120 || componentType === 5121) {
        return 1;
      }
      if (componentType === 5122 || componentType === 5123) {
        return 2;
      }
      if (componentType === 5125 || componentType === 5126) {
        return 4;
      }
      throw new Error("unsupported GLB component type " + componentType);
    }

    function glbNodeMatrix(node) {
      if (node.matrix) {
        return node.matrix.slice();
      }
      const translation = node.translation || [0, 0, 0];
      const rotation = node.rotation || [0, 0, 0, 1];
      const scale = node.scale || [1, 1, 1];
      return mat4FromTRS(translation, rotation, scale);
    }

    function meshFromFlat(positions, normals, color) {
      const linePositions = triangleLines(positions);
      return {
        positions,
        normals,
        linePositions,
        color,
        box: boxFromPositions(positions)
      };
    }

    function triangleLines(positions) {
      const triCount = positions.length / 9;
      const lines = new Float32Array(triCount * 18);
      for (let tri = 0; tri < triCount; tri += 1) {
        const src = tri * 9;
        const dst = tri * 18;
        copyVec3(positions, src, lines, dst);
        copyVec3(positions, src + 3, lines, dst + 3);
        copyVec3(positions, src + 3, lines, dst + 6);
        copyVec3(positions, src + 6, lines, dst + 9);
        copyVec3(positions, src + 6, lines, dst + 12);
        copyVec3(positions, src, lines, dst + 15);
      }
      return lines;
    }

    function resetView() {
      if (!fitBox) {
        return;
      }
      const center = boxCenter(fitBox);
      const size = boxSize(fitBox);
      const maxSize = Math.max(size[0], size[1], size[2], 1);
      const distance = (maxSize / (2 * Math.tan(degToRad(FOV_DEG) / 2))) * 1.65;
      for (const view of views) {
        view.orbit.target = center.slice();
        view.orbit.distance = distance;
        view.orbit.near = Math.max(distance / 1000, 0.01);
        view.orbit.far = distance * 12;
      }
    }

    function syncFrom(source) {
      if (syncing) {
        return;
      }
      syncing = true;
      for (const view of views) {
        if (view === source) {
          continue;
        }
        view.orbit = {
          target: source.orbit.target.slice(),
          distance: source.orbit.distance,
          yaw: source.orbit.yaw,
          pitch: source.orbit.pitch,
          near: source.orbit.near,
          far: source.orbit.far
        };
      }
      syncing = false;
    }

    function setWireframe(enabled) {
      wireframeEnabled = enabled;
    }

    function resizeAll() {
      for (const view of views) {
        resizeView(view);
      }
    }

    function resizeView(view) {
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.floor(view.viewport.clientWidth * pixelRatio));
      const height = Math.max(1, Math.floor(view.viewport.clientHeight * pixelRatio));
      if (view.canvas.width !== width || view.canvas.height !== height) {
        view.canvas.width = width;
        view.canvas.height = height;
      }
      view.canvas.style.width = "100%";
      view.canvas.style.height = "100%";
      view.gl.viewport(0, 0, width, height);
    }

    function render() {
      requestAnimationFrame(render);
      for (const view of views) {
        renderView(view);
      }
    }

    function renderView(view) {
      resizeView(view);
      const gl = view.gl;
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.useProgram(view.program.program);
      const aspect = Math.max(1, view.canvas.width) / Math.max(1, view.canvas.height);
      const viewProj = mat4Multiply(
        mat4Perspective(degToRad(FOV_DEG), aspect, view.orbit.near, view.orbit.far),
        cameraViewMatrix(view.orbit)
      );
      gl.uniformMatrix4fv(view.program.uViewProj, false, new Float32Array(viewProj));
      for (const mesh of view.meshes) {
        drawMesh(view, mesh, wireframeEnabled);
      }
    }

    function drawMesh(view, mesh, wireframe) {
      const gl = view.gl;
      const program = view.program;
      gl.uniform3fv(program.uColor, new Float32Array(mesh.color));
      if (wireframe) {
        gl.disableVertexAttribArray(program.aNormal);
        gl.vertexAttrib3f(program.aNormal, 0, 0, 1);
        gl.bindBuffer(gl.ARRAY_BUFFER, mesh.lineBuffer);
        gl.enableVertexAttribArray(program.aPosition);
        gl.vertexAttribPointer(program.aPosition, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.LINES, 0, mesh.lineCount);
      } else {
        gl.bindBuffer(gl.ARRAY_BUFFER, mesh.positionBuffer);
        gl.enableVertexAttribArray(program.aPosition);
        gl.vertexAttribPointer(program.aPosition, 3, gl.FLOAT, false, 0, 0);
        gl.bindBuffer(gl.ARRAY_BUFFER, mesh.normalBuffer);
        gl.enableVertexAttribArray(program.aNormal);
        gl.vertexAttribPointer(program.aNormal, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.TRIANGLES, 0, mesh.triangleCount);
      }
    }

    function cameraVectors(orbit) {
      const cp = Math.cos(orbit.pitch);
      const sp = Math.sin(orbit.pitch);
      const cy = Math.cos(orbit.yaw);
      const sy = Math.sin(orbit.yaw);
      const forward = vec3Normalize([cp * cy, cp * sy, sp]);
      const eye = vec3Add(orbit.target, vec3Scale(forward, orbit.distance));
      const upWorld = [0, 0, 1];
      const right = vec3Normalize(vec3Cross(forward, upWorld));
      const up = vec3Normalize(vec3Cross(right, forward));
      return { eye, forward, right, up };
    }

    function cameraViewMatrix(orbit) {
      const camera = cameraVectors(orbit);
      return mat4LookAt(camera.eye, orbit.target, [0, 0, 1]);
    }

    function assetErrorMessage(url, error) {
      const rawMessage = error && error.message ? error.message : String(error || "unknown error");
      const fileHint = location.protocol === "file:"
        ? " Browser file access can block model loads; serve this output folder over HTTP or run barprint view."
        : "";
      return "Failed to load " + url + ": " + rawMessage + "." + fileHint;
    }

    function setStatus(view, message, tone) {
      const status = view.pane.querySelector(".status");
      status.textContent = message;
      status.className = tone ? "status " + tone : "status";
      status.style.display = message ? "block" : "none";
    }

    function boxEmpty() {
      return { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
    }

    function boxIsEmpty(box) {
      return !Number.isFinite(box.min[0]) || box.min[0] > box.max[0];
    }

    function boxClone(box) {
      return { min: box.min.slice(), max: box.max.slice() };
    }

    function boxUnion(a, b) {
      if (boxIsEmpty(a)) {
        return boxClone(b);
      }
      if (boxIsEmpty(b)) {
        return boxClone(a);
      }
      return {
        min: [Math.min(a.min[0], b.min[0]), Math.min(a.min[1], b.min[1]), Math.min(a.min[2], b.min[2])],
        max: [Math.max(a.max[0], b.max[0]), Math.max(a.max[1], b.max[1]), Math.max(a.max[2], b.max[2])]
      };
    }

    function boxFromPositions(positions) {
      const box = boxEmpty();
      for (let index = 0; index < positions.length; index += 3) {
        box.min[0] = Math.min(box.min[0], positions[index]);
        box.min[1] = Math.min(box.min[1], positions[index + 1]);
        box.min[2] = Math.min(box.min[2], positions[index + 2]);
        box.max[0] = Math.max(box.max[0], positions[index]);
        box.max[1] = Math.max(box.max[1], positions[index + 1]);
        box.max[2] = Math.max(box.max[2], positions[index + 2]);
      }
      return box;
    }

    function boxCenter(box) {
      return [(box.min[0] + box.max[0]) / 2, (box.min[1] + box.max[1]) / 2, (box.min[2] + box.max[2]) / 2];
    }

    function boxSize(box) {
      return [box.max[0] - box.min[0], box.max[1] - box.min[1], box.max[2] - box.min[2]];
    }

    function writeVec3(array, offset, value) {
      array[offset] = value[0];
      array[offset + 1] = value[1];
      array[offset + 2] = value[2];
    }

    function copyVec3(source, sourceOffset, target, targetOffset) {
      target[targetOffset] = source[sourceOffset];
      target[targetOffset + 1] = source[sourceOffset + 1];
      target[targetOffset + 2] = source[sourceOffset + 2];
    }

    function faceNormal(a, b, c) {
      return vec3Normalize(vec3Cross(vec3Sub(b, a), vec3Sub(c, a)));
    }

    function vec3Add(a, b) {
      return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
    }

    function vec3Sub(a, b) {
      return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
    }

    function vec3Scale(a, scalar) {
      return [a[0] * scalar, a[1] * scalar, a[2] * scalar];
    }

    function vec3Cross(a, b) {
      return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0]
      ];
    }

    function vec3Dot(a, b) {
      return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    }

    function vec3Length(a) {
      return Math.sqrt(vec3Dot(a, a));
    }

    function vec3Normalize(a) {
      const length = vec3Length(a);
      if (length <= 0.000001) {
        return [0, 0, 1];
      }
      return [a[0] / length, a[1] / length, a[2] / length];
    }

    function transformPoint(m, p) {
      return [
        m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
        m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
        m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14]
      ];
    }

    function transformDirection(m, p) {
      return [
        m[0] * p[0] + m[4] * p[1] + m[8] * p[2],
        m[1] * p[0] + m[5] * p[1] + m[9] * p[2],
        m[2] * p[0] + m[6] * p[1] + m[10] * p[2]
      ];
    }

    function mat4Identity() {
      return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
    }

    function mat4Multiply(a, b) {
      const out = new Array(16);
      for (let column = 0; column < 4; column += 1) {
        for (let row = 0; row < 4; row += 1) {
          out[column * 4 + row] =
            a[0 * 4 + row] * b[column * 4 + 0] +
            a[1 * 4 + row] * b[column * 4 + 1] +
            a[2 * 4 + row] * b[column * 4 + 2] +
            a[3 * 4 + row] * b[column * 4 + 3];
        }
      }
      return out;
    }

    function mat4FromTRS(t, q, s) {
      const x = q[0];
      const y = q[1];
      const z = q[2];
      const w = q[3];
      const x2 = x + x;
      const y2 = y + y;
      const z2 = z + z;
      const xx = x * x2;
      const xy = x * y2;
      const xz = x * z2;
      const yy = y * y2;
      const yz = y * z2;
      const zz = z * z2;
      const wx = w * x2;
      const wy = w * y2;
      const wz = w * z2;
      return [
        (1 - (yy + zz)) * s[0], (xy + wz) * s[0], (xz - wy) * s[0], 0,
        (xy - wz) * s[1], (1 - (xx + zz)) * s[1], (yz + wx) * s[1], 0,
        (xz + wy) * s[2], (yz - wx) * s[2], (1 - (xx + yy)) * s[2], 0,
        t[0], t[1], t[2], 1
      ];
    }

    function mat4Perspective(fovy, aspect, near, far) {
      const f = 1 / Math.tan(fovy / 2);
      const nf = 1 / (near - far);
      return [
        f / aspect, 0, 0, 0,
        0, f, 0, 0,
        0, 0, (far + near) * nf, -1,
        0, 0, 2 * far * near * nf, 0
      ];
    }

    function mat4LookAt(eye, target, up) {
      const z = vec3Normalize(vec3Sub(eye, target));
      const x = vec3Normalize(vec3Cross(up, z));
      const y = vec3Cross(z, x);
      return [
        x[0], y[0], z[0], 0,
        x[1], y[1], z[1], 0,
        x[2], y[2], z[2], 0,
        -vec3Dot(x, eye), -vec3Dot(y, eye), -vec3Dot(z, eye), 1
      ];
    }

    function degToRad(degrees) {
      return degrees * Math.PI / 180;
    }

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }
  </script>"""
)
