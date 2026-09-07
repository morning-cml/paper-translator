/* L3 组件层：文件篮 / 设置面板 / 运行区 / 轻提示。
   规则：不直接 fetch（走 api.js）、组件间不互调（走 store.js）。 */
import { api } from "./api.js";
import { store } from "./store.js";

const $ = (id) => document.getElementById(id);

/* ---------------- Toast ---------------- */
let toastTimer = null;
export function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ---------------- FileBasket ---------------- */
export function initFiles() {
  const list = $("file-list");

  function render() {
    const files = store.get("files");
    list.innerHTML = "";
    for (const f of files) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = f;
      name.title = f;
      const rm = document.createElement("button");
      rm.className = "rm";
      rm.textContent = "✕";
      rm.title = "移除";
      rm.addEventListener("click", () =>
        store.set("files", store.get("files").filter((x) => x !== f)));
      li.append(name, rm);
      list.appendChild(li);
    }
  }
  store.on("files", render);

  $("btn-pick").addEventListener("click", async () => {
    const r = await api.pick();
    if (r.error) return toast("选择文件失败：" + r.error);
    if (r.files?.length) {
      const merged = [...new Set([...store.get("files"), ...r.files])];
      store.set("files", merged);
    }
  });
  $("btn-clear").addEventListener("click", () => store.set("files", []));
  render();
}

/* ---------------- SettingsPanel ---------------- */
/* 与 src/gui.py 的 SERVICES 保持一致（改一处必须同步另一处）。
   申请入口：DeepSeek platform.deepseek.com；Kimi platform.kimi.com；
   智谱 open.bigmodel.cn；豆包 console.volcengine.com/ark（需先开通模型）。 */
const SERVICES = [
  { name: "DeepSeek 官方", base: "https://api.deepseek.com",
    models: ["deepseek-v4-pro", "deepseek-v4-flash"] },
  { name: "Kimi（月之暗面）", base: "https://api.moonshot.cn/v1",
    models: ["kimi-k2.6", "kimi-k3"] },
  { name: "智谱 GLM", base: "https://open.bigmodel.cn/api/paas/v4",
    models: ["glm-5.2"] },
  { name: "豆包（火山方舟）", base: "https://ark.cn-beijing.volces.com/api/v3",
    models: ["doubao-seed-2.0-pro", "doubao-seed-2.0-lite"] },
  { name: "OpenAI", base: "https://api.openai.com/v1",
    models: ["gpt-4o-mini", "gpt-4o"] },
  { name: "Ollama 本地（免费离线）", base: "http://127.0.0.1:11434/v1",
    models: ["qwen2.5:7b", "llama3.1:8b"] },
  { name: "自定义", base: "", models: [] },
];
const DOMAINS = ["计算机科学", "通用学术", "生物医学", "物理学", "数学",
                 "电子工程", "化学"];

const CUSTOM = "__custom__";

/* 「下拉 + 自定义」联动：选到"自定义…"时显示旁边的输入框。
   （datalist 会按已填文字过滤选项，导致"看起来只有一个选项"——弃用。） */
function comboSetup(selId, customId) {
  const sel = $(selId), custom = $(customId);
  sel.addEventListener("change", () => {
    custom.hidden = sel.value !== CUSTOM;
    if (!custom.hidden) custom.focus();
  });
}

function comboFill(selId, options, current) {
  const sel = $(selId), custom = $(selId + "-custom");
  sel.innerHTML = "";
  const opts = [...options];
  if (current && !opts.includes(current)) opts.unshift(current);
  for (const o of opts) sel.add(new Option(o, o));
  sel.add(new Option("自定义…", CUSTOM));
  sel.value = current && opts.includes(current) ? current : opts[0] || CUSTOM;
  custom.hidden = sel.value !== CUSTOM;
}

function comboValue(selId) {
  const sel = $(selId);
  return (sel.value === CUSTOM ? $(selId + "-custom").value : sel.value).trim();
}

let LANGS = null;   // 后端语言表 + 推荐（单一真源，避免前后端不一致）

function renderReco() {
  const box = $("lang-reco");
  const tgt = $("s-target").value;
  const r = LANGS && LANGS.recommend && LANGS.recommend[tgt];
  if (!r) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = "";
  const tip = document.createElement("span");
  tip.innerHTML = `💡 译入此语言推荐用 <b>${r.services}</b>：${r.reason}　`;
  box.appendChild(tip);
  // 若推荐里的首个服务名能匹配某个预设，给一个"切换"按钮
  const first = r.services.split("/")[0].trim();
  const idx = SERVICES.findIndex((s) => s.name.includes(first) || first.includes(s.name.replace(/（.*/, "")));
  if (idx >= 0 && +$("s-service").value !== idx) {
    const btn = document.createElement("button");
    btn.className = "reco-switch";
    btn.textContent = `切换到 ${SERVICES[idx].name}`;
    btn.addEventListener("click", () => {
      $("s-service").value = idx;
      $("s-service").dispatchEvent(new Event("change"));
      renderReco();
    });
    box.appendChild(btn);
  }
  const note = document.createElement("div");
  note.className = "reco-note";
  note.textContent = LANGS.note || "";
  box.appendChild(note);
}

export function initSettings() {
  const svc = $("s-service");
  SERVICES.forEach((s, i) => svc.add(new Option(s.name, i)));
  comboSetup("s-model", "s-model-custom");
  comboSetup("s-domain", "s-domain-custom");

  svc.addEventListener("change", () => {
    const s = SERVICES[+svc.value];
    if (s.base) $("s-baseurl").value = s.base;
    if (s.models.length) comboFill("s-model", s.models, s.models[0]);
    renderReco();
  });
  $("s-target").addEventListener("change", renderReco);

  $("btn-eye").addEventListener("click", () => {
    const k = $("s-key");
    k.type = k.type === "password" ? "text" : "password";
  });
  $("btn-test").addEventListener("click", async () => {
    toast("正在测试连接…");
    const r = await api.test(collectSettings());
    toast(r.message || (r.ok ? "连接成功" : "连接失败"));
  });
  $("btn-glossary").addEventListener("click", async () => {
    const r = await api.pick("csv");
    if (r.files?.[0]) $("s-glossary").value = r.files[0];
  });
  $("btn-outdir").addEventListener("click", async () => {
    const r = await api.pick("dir");
    if (r.files?.[0]) {
      $("s-outdir").value = r.files[0];
      $("s-out-custom").checked = true;
    }
  });

  initGuide();

  // 语言表（后端单一真源）+ 配置，并行取
  return Promise.all([api.languages(), api.config()]).then(([langs, cfg]) => {
    LANGS = langs;
    const fillLang = (id, list, cur) => {
      const s = $(id);
      s.innerHTML = "";
      for (const l of list) s.add(new Option(l.name, l.code));
      s.value = list.some((l) => l.code === cur) ? cur : list[0].code;
    };
    fillLang("s-source", langs.sources, cfg.source_lang || "auto");
    fillLang("s-target", langs.targets, cfg.target_lang || "zh");

    $("s-key").value = cfg.api_key || "";
    $("s-remember").checked = !!cfg.api_key;
    // 首次引导：未配置 Key 且用户没手动关掉过，才显示
    const dismissed = localStorage.getItem("guide-dismissed") === "1";
    if (!cfg.api_key && !dismissed) $("guide-card").hidden = false;
    $("s-baseurl").value = cfg.base_url || "";
    $("s-mode").value = cfg.output_mode || "translated";
    $("s-trial").value = cfg.max_pages || 0;
    $("s-page-range").value = cfg.page_range || "";
    $("s-workers").value = cfg.max_workers || 8;
    $("s-backend").value = cfg.render_backend || "auto";
    $("s-thinking").checked = !!cfg.thinking;
    $("s-auto-glossary").checked = cfg.auto_glossary !== false;
    $("s-direct").checked = cfg.use_system_proxy === false;
    $("s-glossary").value = cfg.glossary_path || "glossary/cs_terms.csv";
    const i = SERVICES.findIndex(
      (s) => s.base && (cfg.base_url || "").replace(/\/$/, "") === s.base);
    svc.value = i >= 0 ? i : SERVICES.length - 1;
    const models = i >= 0 ? SERVICES[i].models : [];
    comboFill("s-model", models, cfg.model || models[0] || "");
    comboFill("s-domain", DOMAINS, cfg.domain || DOMAINS[0]);
    renderReco();
  });
}

function initGuide() {
  $("guide-dismiss").addEventListener("click", () => {
    $("guide-card").hidden = true;
    localStorage.setItem("guide-dismissed", "1");
  });
  $("guide-try-mock").addEventListener("click", () => {
    $("s-mock").checked = true;
    $("guide-card").hidden = true;
    $("adv").open = true;                        // 展开高级区，让用户看到勾选状态
    toast("已开启离线测试模式，选文件后点开始即可看排版效果");
  });
}

export function collectSettings() {
  return {
    api_key: $("s-key").value.trim(),
    model: comboValue("s-model"),
    base_url: $("s-baseurl").value.trim() || null,
    output_mode: $("s-mode").value,
    source_lang: $("s-source").value,
    target_lang: $("s-target").value,
    domain: comboValue("s-domain") || null,
    max_pages: Math.max(0, +$("s-trial").value || 0),
    // 指定页码（"1-3,5,8-"）：非空时后端优先用它，忽略 max_pages
    page_range: $("s-page-range").value.trim(),
    max_workers: Math.min(32, Math.max(1, +$("s-workers").value || 8)),
    render_backend: $("s-backend").value,
    thinking: $("s-thinking").checked,
    auto_glossary: $("s-auto-glossary").checked,
    use_system_proxy: !$("s-direct").checked,
    refresh_cache: $("s-refresh").checked,   // 忽略缓存重译（仅本次，不落盘）
    glossary_path: $("s-glossary").value.trim() || null,
  };
}

export function outdirValue() {
  return $("s-out-custom").checked ? $("s-outdir").value.trim() : "";
}

/* ---------------- RunBar + ProgressCard + Results + Log ---------------- */
export function initRun() {
  const bar = $("bar"), pct = $("pct"), log = $("log");
  const status = $("status-line"), results = $("results");
  const progCard = $("progress-card");
  let timer = null, lastLogLen = 0;

  function setRunning(on) {
    store.set("running", on);
    $("btn-start").disabled = on;
    $("btn-cancel").disabled = !on;
    progCard.classList.toggle("running", on);
    document.body.classList.toggle("running", on);
  }

  function showJob(j) {
    bar.style.width = (j.percent || 0) + "%";
    pct.textContent = (j.percent || 0) + "%";
    status.textContent = j.message || "";
    if (j.log.length > lastLogLen) {
      log.textContent += j.log.slice(lastLogLen).join("\n") + "\n";
      lastLogLen = j.log.length;
      log.scrollTop = log.scrollHeight;
    }
    results.innerHTML = "";
    for (const o of j.outputs || []) {
      const li = document.createElement("li");
      const ok = document.createElement("span");
      ok.className = "ok";
      ok.textContent = "✔";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = o.file.split(/[\\/]/).pop();
      const b1 = document.createElement("button");
      b1.className = "btn btn-sm";
      b1.textContent = "打开 PDF";
      b1.addEventListener("click", () => api.open(o.file));
      const b2 = document.createElement("button");
      b2.className = "btn btn-sm";
      b2.textContent = "打开文件夹";
      b2.addEventListener("click", () => api.open(o.file, true));
      li.append(ok, name, b1, b2);
      results.appendChild(li);
    }
  }

  async function poll(id) {
    let j;
    try {
      j = await api.job(id);
    } catch (e) {
      setRunning(false);
      return toast("与本地服务失联（窗口是否被关闭？）");
    }
    if (j.error) { setRunning(false); return toast(j.error); }
    showJob(j);
    if (j.status === "running") {
      timer = setTimeout(() => poll(id), 600);
    } else {
      setRunning(false);
      toast(j.status === "done" ? "翻译完成 ✔" : j.message);
    }
  }

  $("btn-start").addEventListener("click", async () => {
    const files = store.get("files");
    if (!files.length) return toast("请先选择 PDF 文件");
    const overrides = collectSettings();
    const mock = $("s-mock").checked;
    if (!mock && !overrides.api_key) return toast("请填写 API Key（或勾选离线测试）");
    const outdir = outdirValue();
    if ($("s-out-custom").checked && !outdir)
      return toast("请先选择输出文件夹（或改回“原文件同目录”）");
    log.textContent = ""; lastLogLen = 0; results.innerHTML = "";
    // 记住设置；未勾"记住"时 Key 不落盘（仅本次任务使用）
    const saved = { ...overrides };
    if (!$("s-remember").checked) saved.api_key = "";
    delete saved.refresh_cache;   // 忽略缓存是一次性动作，不写入配置
    await api.saveConfig(saved);
    const r = await api.start({ files, overrides, mock, outdir });
    if (r.error) return toast(r.error);
    store.set("jobId", r.job_id);
    setRunning(true);
    poll(r.job_id);
  });

  $("btn-cancel").addEventListener("click", () => {
    const id = store.get("jobId");
    if (!id) return;
    // 立即反馈：禁用按钮 + 文案，别让用户以为"点了没反应"再猛点。
    // 后端会短路队列中未开始的批、并放弃等待在途请求，通常一两秒内停下。
    $("btn-cancel").disabled = true;
    status.textContent = "正在取消…";
    toast("正在取消…（等当前在途请求返回后停止）");
    api.cancel(id);
  });
}
