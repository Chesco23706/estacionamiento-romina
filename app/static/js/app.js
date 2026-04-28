function pad(value) {
  return String(value).padStart(2, "0");
}

function renderDuration(totalSeconds) {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
}

function startLiveTimers() {
  const timers = document.querySelectorAll(".live-timer");
  if (!timers.length) {
    return;
  }

  const tick = () => {
    timers.forEach((timer) => {
      const entryAt = timer.dataset.entryAt;
      if (!entryAt) {
        return;
      }
      const diff = (Date.now() - new Date(entryAt).getTime()) / 1000;
      timer.textContent = renderDuration(diff);
    });
  };

  tick();
  window.setInterval(tick, 1000);
}

window.addEventListener("DOMContentLoaded", startLiveTimers);
