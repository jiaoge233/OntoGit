const API_BASE_URL =
  window.API_BASE_URL || `${window.location.protocol}//${window.location.hostname}:5000`;
const APP_MODE = window.APP_MODE || "probability";
const API_URL =
  APP_MODE === "probability-reason"
    ? `${API_BASE_URL}/api/llm/probability-reason`
    : `${API_BASE_URL}/api/llm/probability`;

const promptInput = document.getElementById("prompt");
const submitBtn = document.getElementById("submitBtn");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const modeTitleEl = document.getElementById("modeTitle");
const modeDescEl = document.getElementById("modeDesc");

function buildRequestBody(message) {
  try {
    const parsed = JSON.parse(message);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed;
    }
  } catch {
    // Non-JSON input is still supported as a plain prompt.
  }
  return { message };
}

if (modeTitleEl && modeDescEl) {
  if (APP_MODE === "probability-reason") {
    document.title = "概率推理 · Reason";
    modeTitleEl.textContent = "概率推理 · Reason";
    modeDescEl.textContent = "输入本体 JSON 或描述文本，返回真实性概率和简明判断原因。";
  } else {
    document.title = "概率推理";
    modeTitleEl.textContent = "概率推理";
    modeDescEl.textContent = "输入本体 JSON 或描述文本，只返回百分比概率。";
  }
}

async function sendMessage() {
  const message = promptInput.value.trim();

  if (!message) {
    statusEl.textContent = "请输入本体 JSON 或描述文本。";
    resultEl.textContent = APP_MODE === "probability-reason"
      ? "这里会显示 probability 和 reason。"
      : "这里会显示后端返回的概率。";
    return;
  }

  submitBtn.disabled = true;
  statusEl.textContent = "正在请求后端并调用模型...";
  resultEl.textContent = "";

  try {
    const response = await fetch(API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(buildRequestBody(message)),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "请求失败");
    }

    resultEl.textContent = data.text || "模型没有返回文本内容。";
    statusEl.textContent = "调用成功。";
  } catch (error) {
    statusEl.textContent = "调用失败。";
    resultEl.textContent = `${error.message || "发生未知错误。"}\n请求地址：${API_URL}`;
    console.error(error);
    resultEl.textContent = "请稍后重试";
  } finally {
    submitBtn.disabled = false;
  }
}

submitBtn.addEventListener("click", sendMessage);

promptInput.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    sendMessage();
  }
});
