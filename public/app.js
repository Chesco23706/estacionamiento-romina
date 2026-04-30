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

function setupPasswordToggles() {
  const toggles = document.querySelectorAll("[data-password-toggle]");
  toggles.forEach((toggle) => {
    const wrapper = toggle.closest(".password-field");
    const input = wrapper ? wrapper.querySelector("[data-password-input]") : null;
    if (!input) {
      return;
    }

    toggle.addEventListener("click", () => {
      const isPassword = input.type === "password";
      input.type = isPassword ? "text" : "password";
      toggle.textContent = isPassword ? "Ocultar" : "Mostrar";
    });
  });
}

function syncSectionFromHash() {
  const hash = window.location.hash;
  if (!hash) {
    return;
  }

  const target = document.querySelector(hash);
  if (!target) {
    return;
  }

  if (target.tagName === "DETAILS") {
    target.open = true;
  }

  const detailsParent = target.closest("details");
  if (detailsParent) {
    detailsParent.open = true;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  startLiveTimers();
  setupPasswordToggles();
  syncSectionFromHash();
});

window.addEventListener("hashchange", syncSectionFromHash);
