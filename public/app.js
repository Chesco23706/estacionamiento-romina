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

function renderDayCounterText(entryAt, contractedDays) {
  const diff = Date.now() - new Date(entryAt).getTime();
  const usedDays = Math.max(1, Math.ceil(diff / 86400000));
  const remainingDays = Math.max(contractedDays - usedDays, 0);
  return `Restan ${remainingDays} de ${contractedDays} dias`;
}

function startLiveDayCounters() {
  const counters = document.querySelectorAll(".live-day-counter");
  if (!counters.length) {
    return;
  }

  const tick = () => {
    counters.forEach((counter) => {
      const entryAt = counter.dataset.entryAt;
      const contractedDays = Number.parseInt(counter.dataset.contractedDays || "1", 10);
      if (!entryAt) {
        return;
      }
      counter.textContent = renderDayCounterText(entryAt, Math.max(contractedDays, 1));
    });
  };

  tick();
  window.setInterval(tick, 60000);
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

function setupRecordModeFields() {
  const modeSelects = document.querySelectorAll("[data-record-mode]");
  modeSelects.forEach((modeSelect) => {
    const form = modeSelect.closest("form");
    const contractedInput = form
      ? form.querySelector("[data-contracted-days-input]")
      : null;
    if (!contractedInput) {
      return;
    }

    const syncField = () => {
      contractedInput.value = modeSelect.value === "weekly" ? "7" : "";
    };

    modeSelect.addEventListener("change", syncField);
    syncField();
  });
}

function setupServiceFields() {
  const serviceToggles = document.querySelectorAll("[data-service-oil-change]");
  serviceToggles.forEach((toggle) => {
    const form = toggle.closest("form");
    const oilPriceField = form ? form.querySelector("[data-oil-price-field]") : null;
    const oilPriceInput = oilPriceField ? oilPriceField.querySelector("input[name='service_oil_price']") : null;
    if (!oilPriceField || !oilPriceInput) {
      return;
    }

    const syncField = () => {
      const enabled = toggle.checked;
      oilPriceField.hidden = !enabled;
      oilPriceInput.required = enabled;
      if (!enabled) {
        oilPriceInput.value = "";
      }
    };

    toggle.addEventListener("change", syncField);
    syncField();
  });
}

function setupDialogs() {
  const openButtons = document.querySelectorAll("[data-open-dialog]");
  const closeButtons = document.querySelectorAll("[data-close-dialog]");

  openButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.openDialog);
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    });
  });

  closeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.closeDialog);
      if (dialog) {
        dialog.close();
      }
    });
  });

  document.querySelectorAll("dialog.record-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      const rect = dialog.getBoundingClientRect();
      const inside =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!inside) {
        dialog.close();
      }
    });
  });
}

window.addEventListener("DOMContentLoaded", () => {
  startLiveTimers();
  startLiveDayCounters();
  setupPasswordToggles();
  setupRecordModeFields();
  setupServiceFields();
  setupDialogs();
  syncSectionFromHash();
});

window.addEventListener("hashchange", syncSectionFromHash);
