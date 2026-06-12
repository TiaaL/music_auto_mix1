#!/usr/bin/env node
// Render template A/B/C using FaustWASM instead of native Faust binaries.
//
// Usage:
//   node scripts/render_template_mix_wasm.mjs template_a vocal.wav accomp.wav out.wav
//   node scripts/render_template_mix_wasm.mjs template_b vocal.wav accomp.wav out.wav --with-volume-automation

import { existsSync, mkdirSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const faustRoot = resolve(root, ".tools", "faustcheck", "node_modules", "@grame", "faustwasm");
const faust2wavUrl = pathToFileURL(join(faustRoot, "src", "faust2wavFiles.js")).href;
const indexUrl = pathToFileURL(join(faustRoot, "dist", "esm", "index.js")).href;
const originalConsoleLog = console.log.bind(console);
const originalStdoutWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = (chunk, encoding, callback) => {
  const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
  const filtered = text
    .split(/\n/)
    .filter((line) => !/^\d+\s+\/\s+\d+$/.test(line.trim()))
    .join("\n");
  if (!filtered) {
    if (typeof callback === "function") {
      callback();
    }
    return true;
  }
  return originalStdoutWrite(filtered, encoding, callback);
};
console.log = (...items) => {
  const line = items.join(" ");
  if (/^\d+\s+\/\s+\d+$/.test(line.trim())) {
    return;
  }
  originalConsoleLog(...items);
};

if (!existsSync(faustRoot)) {
  console.error("Missing @grame/faustwasm local install.");
  console.error("Run: npm.cmd install @grame/faustwasm --prefix .tools/faustcheck");
  process.exit(2);
}

const { default: faust2wavFiles } = await import(faust2wavUrl);
const { WavDecoder, WavEncoder } = await import(indexUrl);

const args = process.argv.slice(2);
const positional = [];
const options = {
  withVolumeAutomation: false,
  loudnessFinalizer: true,
  mixPlan: "",
  referenceAudio: "",
};
for (let i = 0; i < args.length; i += 1) {
  const arg = args[i];
  if (arg === "--with-volume-automation") {
    options.withVolumeAutomation = true;
  } else if (arg === "--no-loudness-finalizer") {
    options.loudnessFinalizer = false;
  } else if (arg === "--mix-plan") {
    options.mixPlan = args[++i] || "";
  } else if (arg === "--reference-audio") {
    options.referenceAudio = args[++i] || "";
  } else {
    positional.push(arg);
  }
}
const [templateId, vocalIn, accompIn, finalOut] = positional;

if (!templateId || !vocalIn || !accompIn || !finalOut) {
  console.error("Usage: node scripts/render_template_mix_wasm.mjs <template_a|template_b|template_c|template_d> <vocal.wav> <accomp.wav> <final.wav> [--with-volume-automation]");
  process.exit(2);
}

const templateChains = {
  template_a: {
    vocal: ["c1_gate", "template_a_vocal_proq3", "c1_comp", "sibilance_mono"],
    vocalGroup: ["vocal_group_fx"],
    accomp: ["template_music_proq3_ab"],
    master: ["template_bus_proq3_ab", "gw_mixcentric_stereo", "master_l2_stereo"],
  },
  template_b: {
    vocal: ["rbass_mono", "f6_rta_mono", "c1_comp", "sibilance_mono", "l1_limiter_mono"],
    vocalGroup: ["vocal_group_fx"],
    accomp: ["template_music_proq3_ab"],
    master: ["template_bus_proq3_ab", "gw_mixcentric_stereo", "master_l2_stereo"],
  },
  template_c: {
    vocal: ["template_c_vocal_proq3", "vocal_rider_mono", "c1_comp", "oneknob_brighter_mono"],
    vocalGroup: ["vocal_group_fx"],
    accomp: ["template_music_proq3_c"],
    master: ["template_bus_proq3_c", "gw_mixcentric_stereo", "master_l2_stereo"],
  },
  template_d: {
    vocal: ["rdeesser", "req6", "c1_comp", "vocal_group_fx"],
    vocalGroup: [],
    accomp: ["accomp_proq3", "accomp_c6_sc", "accomp_l2_stereo"],
    master: ["master_proq3", "master_softclipper", "master_l2_stereo"],
  },
};

const chain = templateChains[templateId];
if (!chain) {
  console.error(`Unsupported template id: ${templateId}`);
  process.exit(2);
}

function wavBufferToArrayBuffer(buffer) {
  return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
}

async function decodeWav(path) {
  const data = await readFile(path);
  return WavDecoder.decode(wavBufferToArrayBuffer(data));
}

function assertChannels(audio, expected, label) {
  if (audio.numberOfChannels !== expected) {
    throw new Error(`${label} must have ${expected} channel(s), got ${audio.numberOfChannels}`);
  }
}

function assertSameSampleRate(a, b) {
  if (a.sampleRate !== b.sampleRate) {
    throw new Error(`Sample-rate mismatch: vocal ${a.sampleRate} Hz, accompaniment ${b.sampleRate} Hz`);
  }
}

async function renderStage(stage, inputPath, outputPath, sampleRate, samples) {
  const dspPath = resolve(root, "src", `${stage}.dsp`);
  if (!existsSync(dspPath)) {
    throw new Error(`Missing DSP source for stage ${stage}: ${dspPath}`);
  }
  console.log(`[run] ${stage}`);
  await faust2wavFiles(
    dspPath,
    inputPath,
    outputPath,
    256,
    sampleRate,
    samples,
    24,
    ["-I", resolve(root, "src")]
  );
}

async function renderChain(stages, inputPath, tempDir, prefix, sampleRate, samples) {
  let current = inputPath;
  for (let i = 0; i < stages.length; i += 1) {
    const stage = stages[i];
    const out = join(tempDir, `${prefix}_${String(i + 1).padStart(2, "0")}_${stage}.wav`);
    await renderStage(stage, current, out, sampleRate, samples);
    current = out;
  }
  return current;
}

function mixStereo(a, b) {
  const length = Math.max(a.length, b.length);
  const out = [new Float32Array(length), new Float32Array(length)];
  for (let ch = 0; ch < 2; ch += 1) {
    const ac = a.channelData[ch] ?? a.channelData[0];
    const bc = b.channelData[ch] ?? b.channelData[0];
    for (let i = 0; i < length; i += 1) {
      out[ch][i] = (ac[i] || 0) + (bc[i] || 0);
    }
  }
  return out;
}

async function writeWav(path, channelData, sampleRate) {
  const encoded = WavEncoder.encode(channelData, { bitDepth: 24, sampleRate });
  await writeFile(path, new Uint8Array(encoded));
}

function pythonCommand() {
  const venvPython = join(root, ".venv", "bin", "python");
  if (existsSync(venvPython)) {
    return venvPython;
  }
  return "python3";
}

function runChecked(command, args, label) {
  const proc = spawnSync(command, args, { stdio: "inherit" });
  if (proc.status !== 0) {
    throw new Error(`${label} failed with code ${proc.status}`);
  }
}

function ffmpegCommand() {
  const found = spawnSync("which", ["ffmpeg"], { encoding: "utf8" });
  return found.status === 0 ? found.stdout.trim() : "ffmpeg";
}

function ffprobeSampleRate(path) {
  const proc = spawnSync(
    "ffprobe",
    ["-v", "error", "-show_entries", "stream=sample_rate", "-of", "default=noprint_wrappers=1:nokey=1", path],
    { encoding: "utf8" }
  );
  if (proc.status !== 0) {
    throw new Error(`ffprobe sample-rate failed for ${path}`);
  }
  return Number(proc.stdout.trim());
}

function convertAudio(input, output, sampleRate, channels) {
  runChecked(
    ffmpegCommand(),
    ["-y", "-hide_banner", "-nostats", "-i", input, "-ar", String(sampleRate), "-ac", String(channels), output],
    `ffmpeg convert ${basename(input)}`
  );
}

function applySourceEq(section, input, output) {
  if (!options.mixPlan) {
    return input;
  }
  runChecked(
    pythonCommand(),
    [resolve(root, "scripts", "apply_plan_source_eq.py"), input, output, "--plan", options.mixPlan, "--section", section],
    `source EQ ${section}`
  );
  return output;
}

function applyMasterTilt(input, output) {
  if (!options.mixPlan) {
    return input;
  }
  runChecked(
    pythonCommand(),
    [resolve(root, "scripts", "apply_master_tilt_eq.py"), input, output, "--plan", options.mixPlan],
    "master tilt EQ"
  );
  return output;
}

function planBusGains() {
  if (!options.mixPlan) {
    return { vocal: 0, accomp: 0 };
  }
  const proc = spawnSync(
    pythonCommand(),
    [resolve(root, "scripts", "plan_bus_gains.py"), options.mixPlan],
    { encoding: "utf8" }
  );
  if (proc.status !== 0) {
    return { vocal: 0, accomp: 0 };
  }
  const [vocal, accomp] = proc.stdout.trim().split(/\s+/).map(Number);
  return {
    vocal: Number.isFinite(vocal) ? vocal : 0,
    accomp: Number.isFinite(accomp) ? accomp : 0,
  };
}

function dbToLinear(db) {
  return Math.pow(10, db / 20);
}

async function runVolumeAutomationIfRequested(tempDir) {
  if (!options.withVolumeAutomation) {
    return { vocal: vocalIn, accomp: accompIn };
  }
  const vocalOut = join(tempDir, "auto_vocal.wav");
  const accompOut = join(tempDir, "auto_accomp.wav");
  runChecked(
    pythonCommand(),
    [
      resolve(root, "scripts", "auto_volume_mix.py"),
      vocalIn,
      accompIn,
      "--vocal-out",
      vocalOut,
      "--accomp-out",
      accompOut,
    ],
    "auto_volume_mix.py"
  );
  return { vocal: vocalOut, accomp: accompOut };
}

const tempDir = await mkdtemp(join(tmpdir(), "template-wasm-"));
try {
  const inputs = await runVolumeAutomationIfRequested(tempDir);
  const targetSampleRate = ffprobeSampleRate(inputs.accomp);
  const vocalPrepared = join(tempDir, "prepared_vocal.wav");
  const accompPrepared = join(tempDir, "prepared_accomp.wav");
  convertAudio(inputs.vocal, vocalPrepared, targetSampleRate, 1);
  convertAudio(inputs.accomp, accompPrepared, targetSampleRate, 2);

  const vocalAudio = await decodeWav(vocalPrepared);
  const accompAudio = await decodeWav(accompPrepared);
  assertChannels(vocalAudio, 1, "vocal input");
  assertChannels(accompAudio, 2, "accompaniment input");
  assertSameSampleRate(vocalAudio, accompAudio);

  const sampleRate = vocalAudio.sampleRate;
  const samples = Math.max(vocalAudio.length, accompAudio.length);
  console.log(`[template] ${templateId}`);
  console.log(`[input] vocal=${basename(vocalIn)} accomp=${basename(accompIn)} sr=${sampleRate} samples=${samples}`);

  let vocalRendered = await renderChain(chain.vocal, vocalPrepared, tempDir, "vocal", sampleRate, samples);
  vocalRendered = applySourceEq("vocal_eq", vocalRendered, join(tempDir, "vocal_source_eq.wav"));
  vocalRendered = await renderChain(chain.vocalGroup, vocalRendered, tempDir, "vocal_group", sampleRate, samples);

  let accompRendered = await renderChain(chain.accomp, accompPrepared, tempDir, "accomp", sampleRate, samples);
  accompRendered = applySourceEq("accomp_eq", accompRendered, join(tempDir, "accomp_source_eq.wav"));

  const vocalBus = await decodeWav(vocalRendered);
  const accompBus = await decodeWav(accompRendered);
  assertChannels(vocalBus, 2, "vocal bus");
  assertChannels(accompBus, 2, "accompaniment bus");

  const busGains = planBusGains();
  for (const channel of vocalBus.channelData) {
    const gain = dbToLinear(busGains.vocal);
    for (let i = 0; i < channel.length; i += 1) {
      channel[i] *= gain;
    }
  }
  for (const channel of accompBus.channelData) {
    const gain = dbToLinear(busGains.accomp);
    for (let i = 0; i < channel.length; i += 1) {
      channel[i] *= gain;
    }
  }
  console.log(`[bus] vocal ${busGains.vocal.toFixed(3)} dB, accomp ${busGains.accomp.toFixed(3)} dB`);

  const mixPath = join(tempDir, "stereo_out_sum.wav");
  await writeWav(mixPath, mixStereo(vocalBus, accompBus), sampleRate);
  const mixTilted = applyMasterTilt(mixPath, join(tempDir, "stereo_out_tilted.wav"));

  const masterRendered = await renderChain(chain.master, mixTilted, tempDir, "master", sampleRate, samples);
  const masterAudio = await decodeWav(masterRendered);
  mkdirSync(dirname(finalOut), { recursive: true });
  await writeWav(finalOut, masterAudio.channelData, sampleRate);
  console.log(`[done] ${finalOut}`);
} finally {
  await rm(tempDir, { recursive: true, force: true });
}
