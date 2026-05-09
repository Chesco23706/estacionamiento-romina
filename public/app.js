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
  const servicePanels = document.querySelectorAll("[data-service-panel]");
  servicePanels.forEach((panel) => {
    const body = panel.querySelector("[data-service-panel-body]");
    const toggleButton = panel.querySelector("[data-service-panel-toggle]");
    const washInput = panel.querySelector("[data-service-wash]");
    const oilToggle = panel.querySelector("[data-service-oil-change]");
    const oilPriceField = panel.querySelector("[data-oil-price-field]");
    const oilPriceInput = oilPriceField ? oilPriceField.querySelector("input[name='service_oil_price']") : null;
    if (!body || !toggleButton || !oilToggle || !oilPriceField || !oilPriceInput) {
      return;
    }

    const syncOilField = () => {
      const enabled = oilToggle.checked && !body.hidden;
      oilPriceField.hidden = !enabled;
      oilPriceInput.required = enabled;
      if (!enabled && !body.hidden) {
        oilPriceInput.value = "";
      }
    };

    const syncPanelButton = () => {
      const open = !body.hidden;
      toggleButton.textContent = open ? "Ocultar servicios" : "Agregar servicios";
      toggleButton.setAttribute("aria-expanded", open ? "true" : "false");
      panel.classList.toggle("is-open", open);
    };

    const setPanelOpen = (open) => {
      body.hidden = !open;
      syncOilField();
      syncPanelButton();
    };

    toggleButton.addEventListener("click", () => {
      setPanelOpen(body.hidden);
    });

    oilToggle.addEventListener("change", syncOilField);
    syncOilField();
    syncPanelButton();
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

function setupThemeToggle() {
  const toggle = document.querySelector("[data-theme-toggle]");
  if (!toggle) {
    return;
  }

  const isIconOnly = toggle.classList.contains("employee-nav-btn");

  const applyTheme = (theme) => {
    document.body.dataset.theme = theme;
    const label = theme === "dark" ? "Modo claro" : "Modo oscuro";
    if (isIconOnly) {
      toggle.setAttribute("title", label);
      toggle.setAttribute("aria-label", label);
    } else {
      toggle.textContent = label;
    }
  };

  const storedTheme = window.localStorage.getItem("romina-theme") || "light";
  applyTheme(storedTheme);

  toggle.addEventListener("click", () => {
    const nextTheme = document.body.dataset.theme === "dark" ? "light" : "dark";
    window.localStorage.setItem("romina-theme", nextTheme);
    applyTheme(nextTheme);
  });
}

function setupLiveClock() {
  const clock = document.querySelector("[data-live-clock]");
  const date = document.querySelector("[data-live-date]");
  if (!clock && !date) {
    return;
  }

  const render = () => {
    const now = new Date();
    if (clock) {
      clock.textContent = now.toLocaleTimeString("es-MX", {
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    if (date) {
      date.textContent = now.toLocaleDateString("es-MX", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      });
    }
  };

  render();
  window.setInterval(render, 1000);
}

function setupFriendlyValidation() {
  document.querySelectorAll("[data-friendly-form] input[required], [data-friendly-form] textarea[required], [data-friendly-form] select[required]").forEach((field) => {
    const fieldName = field.dataset.friendlyName || "este dato";
    field.addEventListener("invalid", () => {
      if (field.validity.valueMissing) {
        field.setCustomValidity(`Falta ingresar ${fieldName}.`);
      } else {
        field.setCustomValidity("Revisa este dato.");
      }
    });

    field.addEventListener("input", () => {
      field.setCustomValidity("");
    });

    field.addEventListener("change", () => {
      field.setCustomValidity("");
    });
  });
}

function setupLiveSearchCards() {
  const input = document.querySelector("[data-live-search-input]");
  const cards = document.querySelectorAll("[data-record-search-card]");
  if (!input || !cards.length) {
    return;
  }

  const filterCards = () => {
    const query = input.value.trim().toLowerCase();
    cards.forEach((card) => {
      const haystack = card.dataset.searchText || "";
      const visible = !query || haystack.includes(query);
      card.hidden = !visible;
    });
  };

  input.addEventListener("input", filterCards);
  filterCards();
}

window.addEventListener("DOMContentLoaded", () => {
  startLiveTimers();
  startLiveDayCounters();
  setupPasswordToggles();
  setupRecordModeFields();
  setupServiceFields();
  setupDialogs();
  setupThemeToggle();
  setupLiveClock();
  setupFriendlyValidation();
  setupLiveSearchCards();
  syncSectionFromHash();
});

window.addEventListener("hashchange", syncSectionFromHash);
