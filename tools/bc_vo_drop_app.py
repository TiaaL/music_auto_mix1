#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file


ROOT = Path(__file__).resolve().parent.parent
AUTO_MIX = ROOT / "scripts" / "auto_volume_mix.py"
VOCAL_FX = ROOT / "scripts" / "vocal_stereo_group.sh"
FULL_MIX = ROOT / "scripts" / "full_fx_mix.sh"
L2_ARC = ROOT / "build" / "l2_arc"
DEFAULT_CONFIG = ROOT / "config" / "bc_vo_mix_rules.json"
RUNS_DIR = ROOT / "audio_data" / "web_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return name


FFMPEG = command_path("ffmpeg")


INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BC / VO Audio Runner</title>
  <style>
    :root {
      --bg: #f3eadb;
      --panel: #fff9f1;
      --ink: #191612;
      --muted: #6f6558;
      --line: #dbc9b1;
      --accent: #d35f2d;
      --accent2: #2a6268;
      --ok: #245b31;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "PingFang SC", "Noto Sans SC", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(211,95,45,0.15), transparent 30%),
        radial-gradient(circle at top right, rgba(42,98,104,0.14), transparent 28%),
        linear-gradient(180deg, #fffaf4 0%, var(--bg) 100%);
    }
    .wrap {
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 40px;
      letter-spacing: 0.02em;
    }
    p { color: var(--muted); line-height: 1.6; }
    .grid {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 18px;
      margin-top: 22px;
    }
    .panel {
      background: rgba(255,249,241,0.92);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      box-shadow: 0 18px 44px rgba(58, 35, 10, 0.08);
    }
    .panel h2 { margin: 0 0 12px; font-size: 20px; }
    .drops {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 14px;
    }
    .dropzone {
      min-height: 180px;
      padding: 16px;
      border: 2px dashed var(--line);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.78), rgba(255,247,238,0.98));
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      text-align: center;
      transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease;
    }
    .dropzone.dragover {
      border-color: var(--accent);
      transform: translateY(-2px);
      background: linear-gradient(180deg, rgba(255,245,238,0.95), rgba(255,239,228,1));
    }
    .dropzone strong { font-size: 20px; margin-bottom: 4px; }
    .dropzone small { color: var(--muted); }
    .filename {
      margin-top: 10px;
      font-size: 13px;
      color: var(--accent2);
      word-break: break-all;
    }
    .row { margin-bottom: 14px; }
    label {
      display: block;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 8px;
    }
    input[type="file"], input[type="text"], textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fffdf8;
      padding: 12px 14px;
      font-size: 14px;
    }
    textarea {
      min-height: 220px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      line-height: 1.45;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }
    button {
      border: none;
      border-radius: 999px;
      padding: 12px 18px;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: white;
    }
    button.alt { background: var(--accent2); }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .status {
      min-height: 26px;
      margin-top: 12px;
      color: var(--ok);
      font-size: 14px;
      white-space: pre-wrap;
    }
    .links a {
      display: inline-block;
      margin-right: 12px;
      margin-top: 8px;
      color: var(--accent2);
      font-weight: 700;
      text-decoration: none;
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .drops { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>BC / VO 直接拖进去跑</h1>
    <p>把 <code>vo</code> 人声和 <code>bc</code> 伴奏拖进来，必要时粘贴一份规则 JSON。执行顺序是：规则处理两个 stem，再按原链路分别挂效果器，最后混合导出。</p>

    <div class="grid">
      <section class="panel">
        <h2>音频输入</h2>
        <div class="drops">
          <div class="dropzone" id="vo-zone">
            <strong>VO 人声</strong>
            <small>拖入人声音频，或点击选择</small>
            <input type="file" id="vo-file" accept=".wav,.aif,.aiff,.flac,.mp3,.m4a" hidden />
            <div class="filename" id="vo-name">未选择文件</div>
          </div>
          <div class="dropzone" id="bc-zone">
            <strong>BC 伴奏</strong>
            <small>拖入伴奏音频，或点击选择</small>
            <input type="file" id="bc-file" accept=".wav,.aif,.aiff,.flac,.mp3,.m4a" hidden />
            <div class="filename" id="bc-name">未选择文件</div>
          </div>
        </div>

        <div class="row" style="margin-top:16px;">
          <label for="mix-name">结果命名前缀</label>
          <input type="text" id="mix-name" placeholder="例如 song_01" />
        </div>

        <div class="actions">
          <button id="run-btn">开始处理</button>
        </div>

        <div class="status" id="status"></div>
        <div class="links" id="links"></div>
      </section>

      <section class="panel">
        <h2>规则 JSON</h2>
        <div class="row">
          <label for="config-file">上传自定义配置文件</label>
          <input type="file" id="config-file" accept=".json" />
        </div>
        <div class="row">
          <label for="config-text">或者直接粘贴 / 修改 JSON</label>
          <textarea id="config-text">{{ default_config }}</textarea>
        </div>
        <div class="actions">
          <button class="alt" id="reset-btn">恢复默认规则</button>
        </div>
      </section>
    </div>
  </div>

  <script>
    const voInput = document.getElementById("vo-file");
    const bcInput = document.getElementById("bc-file");
    const configFile = document.getElementById("config-file");
    const configText = document.getElementById("config-text");
    const runBtn = document.getElementById("run-btn");
    const statusEl = document.getElementById("status");
    const linksEl = document.getElementById("links");
    const defaultConfigText = configText.value;

    function wireDrop(zoneId, input, nameId) {
      const zone = document.getElementById(zoneId);
      const name = document.getElementById(nameId);
      zone.addEventListener("click", () => input.click());
      input.addEventListener("change", () => {
        name.textContent = input.files[0] ? input.files[0].name : "未选择文件";
      });
      ["dragenter", "dragover"].forEach(evt => zone.addEventListener(evt, e => {
        e.preventDefault();
        zone.classList.add("dragover");
      }));
      ["dragleave", "drop"].forEach(evt => zone.addEventListener(evt, e => {
        e.preventDefault();
        zone.classList.remove("dragover");
      }));
      zone.addEventListener("drop", e => {
        const files = e.dataTransfer.files;
        if (files && files.length) {
          input.files = files;
          name.textContent = files[0].name;
        }
      });
    }

    wireDrop("vo-zone", voInput, "vo-name");
    wireDrop("bc-zone", bcInput, "bc-name");

    configFile.addEventListener("change", async () => {
      const file = configFile.files[0];
      if (!file) return;
      configText.value = await file.text();
    });

    document.getElementById("reset-btn").addEventListener("click", () => {
      configText.value = defaultConfigText;
      configFile.value = "";
    });

    runBtn.addEventListener("click", async () => {
      const vo = voInput.files[0];
      const bc = bcInput.files[0];
      if (!vo || !bc) {
        statusEl.textContent = "请先把 vo 和 bc 两个音频都放进来。";
        return;
      }

      let parsed;
      try {
        parsed = JSON.parse(configText.value);
      } catch (err) {
        statusEl.textContent = "规则 JSON 格式不对，请先修正。";
        return;
      }

      const form = new FormData();
      form.append("vo_file", vo);
      form.append("bc_file", bc);
      form.append("config_json", JSON.stringify(parsed));
      form.append("mix_name", document.getElementById("mix-name").value.trim());

      runBtn.disabled = true;
      linksEl.innerHTML = "";
      statusEl.textContent = "处理中，请稍等...";

      const resp = await fetch("/run", { method: "POST", body: form });
      const data = await resp.json();
      runBtn.disabled = false;

      if (!resp.ok) {
        statusEl.textContent = data.error || "处理失败";
        return;
      }

      statusEl.textContent = data.log;
      linksEl.innerHTML = `
        <a href="${data.vo_rule_url}">下载规则后 VO</a>
        <a href="${data.bc_rule_url}">下载规则后 BC</a>
        <a href="${data.vo_fx_url}">下载效果后 VO</a>
        <a href="${data.bc_fx_url}">下载效果后 BC</a>
        <a href="${data.config_url}">下载本次规则 JSON</a>
      `;
      if (data.mix_url) {
        linksEl.innerHTML += `<a href="${data.mix_url}">下载混合结果</a>`;
      }
    });
  </script>
</body>
</html>
"""


def safe_name(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text.strip())
    return cleaned.strip("_") or "run"


def write_upload(file_storage, target: Path) -> None:
    file_storage.save(target)


def run_checked(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, cwd=ROOT)


@app.get("/")
def index() -> str:
    return render_template_string(
        INDEX_HTML,
        default_config=DEFAULT_CONFIG.read_text(encoding="utf-8"),
    )


@app.post("/run")
def run_job() -> Response:
    vo_file = request.files.get("vo_file")
    bc_file = request.files.get("bc_file")
    config_json = request.form.get("config_json", "")
    mix_name = safe_name(request.form.get("mix_name", ""))

    if not vo_file or not bc_file:
      return jsonify({"error": "vo_file 和 bc_file 都是必填的。"}), 400

    try:
      parsed = json.loads(config_json)
    except json.JSONDecodeError:
      return jsonify({"error": "配置 JSON 解析失败。"}), 400

    run_id = f"{mix_name}_{uuid.uuid4().hex[:8]}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    vo_in = run_dir / f"vo_input{Path(vo_file.filename or '').suffix or '.wav'}"
    bc_in = run_dir / f"bc_input{Path(bc_file.filename or '').suffix or '.wav'}"
    vo_rule = run_dir / f"{mix_name}_vo_rule.wav"
    bc_rule = run_dir / f"{mix_name}_bc_rule.wav"
    vo_fx = run_dir / f"{mix_name}_vo_fx.wav"
    bc_fx = run_dir / f"{mix_name}_bc_fx.wav"
    mix_out = run_dir / f"{mix_name}_mix.wav"
    cfg_path = run_dir / f"{mix_name}_rules.json"

    write_upload(vo_file, vo_in)
    write_upload(bc_file, bc_in)
    cfg_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cmd = [
        "python3",
        str(AUTO_MIX),
        str(vo_in),
        str(bc_in),
        "--config",
        str(cfg_path),
        "--vocal-out",
        str(vo_rule),
        "--accomp-out",
        str(bc_rule),
    ]

    step1 = run_checked(cmd)
    if step1.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return jsonify({"error": step1.stderr or step1.stdout or "规则处理失败。"}), 500

    step2 = run_checked([str(VOCAL_FX), str(vo_rule), str(vo_fx)])
    if step2.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return jsonify({"error": step2.stderr or step2.stdout or "VO 效果器处理失败。"}), 500

    step3 = run_checked([str(L2_ARC), str(bc_rule), str(bc_fx)])
    if step3.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return jsonify({"error": step3.stderr or step3.stdout or "BC 效果器处理失败。"}), 500

    mix_cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-i",
        str(vo_fx),
        "-i",
        str(bc_fx),
        "-filter_complex",
        "[0:a][1:a]amix=inputs=2:normalize=0[m]",
        "-map",
        "[m]",
        str(mix_out),
    ]
    step4 = run_checked(mix_cmd)
    if step4.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return jsonify({"error": step4.stderr or step4.stdout or "最终混合失败。"}), 500

    combined_log = "\n\n".join(
        part.strip()
        for part in [step1.stdout, step2.stdout, step3.stdout]
        if part and part.strip()
    )

    return jsonify(
        {
            "log": combined_log or "处理完成。",
            "vo_rule_url": f"/download/{run_id}/{vo_rule.name}",
            "bc_rule_url": f"/download/{run_id}/{bc_rule.name}",
            "vo_fx_url": f"/download/{run_id}/{vo_fx.name}",
            "bc_fx_url": f"/download/{run_id}/{bc_fx.name}",
            "mix_url": f"/download/{run_id}/{mix_out.name}" if mix_out.exists() else "",
            "config_url": f"/download/{run_id}/{cfg_path.name}",
        }
    )


@app.get("/download/<run_id>/<filename>")
def download(run_id: str, filename: str):
    path = RUNS_DIR / run_id / filename
    if not path.exists():
        return jsonify({"error": "文件不存在。"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


def main() -> int:
    print("BC / VO drop runner: http://127.0.0.1:7860")
    app.run(host="127.0.0.1", port=7860, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
