<script setup>
import { onBeforeUnmount, ref } from "vue";

const backendHttp = import.meta.env.VITE_BACKEND_HTTP_URL || "http://127.0.0.1:8001";
const backendWs = import.meta.env.VITE_BACKEND_WS_URL || "ws://127.0.0.1:8001";

const task = ref("");
const runId = ref("");
const events = ref([]);
const waitingApproval = ref(false);
const skipGuardConfirmations = ref(false);
const lastGuardRequest = ref(null);
const isRunning = ref(false);
let socket = null;
let pingTimer = null;

function formatArgs(args) {
  if (!args || typeof args !== "object") return "";
  return Object.entries(args)
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(", ");
}

function formatEvent(event) {
  const step = event.step ? `Шаг ${event.step}. ` : "";

  if (event.type === "thought") {
    return `${step}Мысль: ${event.text || ""}`;
  }
  if (event.type === "action") {
    const argsText = formatArgs(event.args);
    return `${step}Действие: ${event.tool || "unknown"}${argsText ? ` (${argsText})` : ""}`;
  }
  if (event.type === "observation") {
    return `${step}Наблюдение: ${event.text || ""}`;
  }
  if (event.type === "error") {
    const parts = [];
    parts.push(`Ошибка: ${event.text || event.error_message || "Неизвестная ошибка"}`);
    if (event.error_type) {
      parts.push(`Тип: ${event.error_type}`);
    }
    if (event.error_repr) {
      parts.push(`repr: ${event.error_repr}`);
    }
    if (event.traceback) {
      parts.push(`Traceback:\n${event.traceback}`);
    }
    return parts.join("\n");
  }
  if (event.type === "guard_request") {
    const base = event.reason || "Потенциально опасное действие";
    const toolLine = event.tool ? `\nИнструмент: ${event.tool}` : "";
    const argsText = formatArgs(event.args);
    const argsLine = argsText ? `\nПараметры: ${argsText}` : "";
    return `${step}Требуется подтверждение: ${base}${toolLine}${argsLine}`;
  }
  if (event.type === "result") {
    return `Результат: ${event.result || ""}`;
  }
  if (event.type === "system") {
    const runPart = event.run_id ? ` (run: ${event.run_id})` : "";
    return `Система: ${event.text || ""}${runPart}`;
  }

  return `Событие: ${event.type || "unknown"}`;
}

function pushEvent(evt) {
  events.value.unshift(evt);
  if (events.value.length > 300) {
    events.value = events.value.slice(0, 300);
  }
  if (evt.type === "guard_request") {
    waitingApproval.value = true;
    lastGuardRequest.value = evt;
  }
  if (evt.type === "system" && evt.text === "Run finished" && evt.run_id === runId.value) {
    isRunning.value = false;
  }
}

function closeSocket() {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
  if (socket) {
    try {
      socket.close();
    } catch {
      // ignore
    }
    socket = null;
  }
}

async function startTask() {
  closeSocket();
  events.value = [];
  waitingApproval.value = false;
  lastGuardRequest.value = null;
  isRunning.value = true;
  const res = await fetch(`${backendHttp}/api/task/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task: task.value,
      skip_guard_confirmations: skipGuardConfirmations.value,
    }),
  });
  const data = await res.json();
  runId.value = data.run_id;
  openSocket();
}

function openSocket() {
  if (!runId.value) return;
  socket = new WebSocket(`${backendWs}/ws/${runId.value}`);
  socket.onmessage = (event) => {
    pushEvent(JSON.parse(event.data));
  };
  socket.onclose = () => {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  };
  socket.onerror = () => {
    isRunning.value = false;
  };
  pingTimer = setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send("ping");
    }
  }, 5000);
}

async function approve(value) {
  if (!runId.value) return;
  await fetch(`${backendHttp}/api/task/${runId.value}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved: value }),
  });
  waitingApproval.value = false;
  lastGuardRequest.value = null;
}

onBeforeUnmount(() => {
  closeSocket();
});
</script>

<template>
  <main class="container">
    <h1>AI Web Agent (MCP Host)</h1>
    <div class="row">
      <input v-model="task" placeholder="Опишите задачу..." />
      <button :disabled="!task || isRunning" @click="startTask">
        {{ isRunning ? "Выполняется..." : "Запустить" }}
      </button>
    </div>
    <label class="skip-guard">
      <input v-model="skipGuardConfirmations" type="checkbox" />
      Не спрашивать подтверждения опасных действий в этом запуске
    </label>
    <p v-if="runId">Run ID: {{ runId }}</p>

    <div v-if="waitingApproval" class="guard">
      <p class="guard-title">Требуется подтверждение</p>
      <p v-if="lastGuardRequest" class="guard-detail">{{ formatEvent(lastGuardRequest) }}</p>
      <p v-else class="guard-detail">Подтвердите или отклоните действие ниже.</p>
      <button @click="approve(true)">Разрешить</button>
      <button @click="approve(false)">Отклонить</button>
    </div>

    <section class="logs">
      <h2>Лента событий</h2>
      <div v-for="(event, idx) in events" :key="idx" class="event">
        <div class="event-type">{{ event.type || "unknown" }}</div>
        <div class="event-text">{{ formatEvent(event) }}</div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.container {
  max-width: 900px;
  margin: 0 auto;
  padding: 20px;
  font-family: Arial, sans-serif;
}
.row {
  display: flex;
  gap: 8px;
}
input {
  flex: 1;
  padding: 8px;
}
button {
  padding: 8px 12px;
}
.skip-guard {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  font-size: 14px;
  cursor: pointer;
}
.guard {
  margin: 14px 0;
  border: 1px solid #f59e0b;
  padding: 10px;
}
.guard-title {
  font-weight: bold;
  margin: 0 0 8px;
}
.guard-detail {
  margin: 0 0 12px;
  white-space: pre-wrap;
  line-height: 1.45;
  font-size: 14px;
}
.logs {
  margin-top: 18px;
}
.event {
  border-bottom: 1px solid #ddd;
  padding: 8px 0;
}
.event-type {
  font-size: 12px;
  color: #64748b;
  text-transform: uppercase;
  margin-bottom: 4px;
}
.event-text {
  white-space: pre-wrap;
  line-height: 1.4;
}
</style>
