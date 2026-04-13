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
const modeNavEl = document.getElementById("modeNav");

if (modeTitleEl && modeDescEl && modeNavEl) {
  if (APP_MODE === "probability-reason") {
    document.title = "Probability Reason";
    modeTitleEl.textContent = "Probability Reason";
    modeDescEl.textContent = "输出概率和原因。";
    modeNavEl.innerHTML =
      '<a href="../probability/">切换到 Probability</a> <span>|</span> <strong>当前：Probability Reason</strong>';
  } else {
    document.title = "Probability";
    modeTitleEl.textContent = "Probability";
    modeDescEl.textContent = "只输出百分比概率。";
    modeNavEl.innerHTML =
      '<strong>当前：Probability</strong> <span>|</span> <a href="../probability-reason/">切换到 Probability Reason</a>';
  }
}

async function sendMessage() {
  const message = promptInput.value.trim();

  if (!message) {
    statusEl.textContent = "请输入用户提示词。";
    resultEl.textContent = "这里会显示后端返回的内容。";
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
      body: JSON.stringify({ message }),
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
