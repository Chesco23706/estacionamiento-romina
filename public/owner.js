"use strict";

const ownerPage = document.querySelector("[data-refresh]");
const workerReady = "serviceWorker" in navigator && window.isSecureContext
    ? navigator.serviceWorker.register(document.body.dataset.worker, { scope: "/owner" })
        .then(() => navigator.serviceWorker.ready).catch(() => null)
    : Promise.resolve(null);

if (ownerPage) {
    const connection = document.querySelector("#connection-status");
    let refreshing = false;

    async function refreshDashboard() {
        if (document.hidden || refreshing) return;
        refreshing = true;
        try {
            const url = new URL(location.href);
            url.searchParams.set("fragment", "1");
            const response = await fetch(url, { cache: "no-store", redirect: "manual" });
            if (response.type === "opaqueredirect" || response.status === 403) {
                ownerPage.hidden = true;
                connection.textContent = "La sesión terminó. Vuelve a abrir la aplicación para entrar.";
                connection.hidden = false;
                return;
            }
            if (!response.ok) throw new Error("No se pudo actualizar.");
            const next = document.createElement("template");
            next.innerHTML = await response.text();
            for (const item of ownerPage.querySelectorAll("details[id]")) {
                const replacement = next.content.querySelector("#" + CSS.escape(item.id));
                if (replacement) replacement.open = item.open;
            }
            const active = document.activeElement;
            const focusedDetail = active?.closest("details")?.id;
            const focusedLink = active?.closest("a")?.getAttribute("href");
            const currentPicker = active?.closest(".day-picker") ? ".day-picker" : "";
            if (ownerPage.innerHTML.trim() !== next.innerHTML.trim()) {
                ownerPage.replaceChildren(next.content);
                if (focusedDetail) {
                    ownerPage.querySelector("#" + CSS.escape(focusedDetail) + " summary")?.focus({ preventScroll: true });
                } else if (focusedLink && !active.isConnected) {
                    ownerPage.querySelector(currentPicker + ' a[href="' + CSS.escape(focusedLink) + '"]')?.focus({ preventScroll: true });
                }
            }
            ownerPage.hidden = false;
            connection.hidden = true;
        } catch {
            connection.textContent = "Sin conexión con Romina. Se muestran los últimos datos cargados.";
            connection.hidden = false;
        } finally {
            refreshing = false;
        }
    }

    function selectTab() {
        const selected = /^#(cortes|shift-)/.test(location.hash) ? "cortes" : "semana";
        for (const tab of document.querySelectorAll("[data-tab]")) {
            if (tab.dataset.tab === selected) tab.setAttribute("aria-current", "page");
            else tab.removeAttribute("aria-current");
        }
    }

    window.addEventListener("hashchange", selectTab);
    window.addEventListener("online", refreshDashboard);
    document.addEventListener("visibilitychange", refreshDashboard);
    window.setInterval(refreshDashboard, 45000);
    if (!location.hash) {
        const linked = ownerPage.querySelector("[data-linked-cut]")?.closest(".shift");
        if (linked) location.hash = linked.id;
    }
    selectTab();
}

const installButton = document.querySelector("#install-button");
if (installButton) {
    const installHelp = document.querySelector("#install-help");
    let installPrompt;
    if (window.matchMedia("(display-mode: standalone)").matches || navigator.standalone) {
        installHelp.hidden = true;
    }
    window.addEventListener("beforeinstallprompt", (event) => {
        event.preventDefault();
        installPrompt = event;
        installButton.hidden = false;
    });
    installButton.addEventListener("click", async () => {
        if (!installPrompt) return;
        await installPrompt.prompt();
        installPrompt = null;
        installButton.hidden = true;
    });
    window.addEventListener("appinstalled", () => {
        installHelp.hidden = true;
        installButton.hidden = true;
    });
}

const push = document.querySelector("#avisos[data-public-key]");
if (push?.dataset.publicKey) {
    const status = document.querySelector("#push-status");
    const enable = document.querySelector("#push-enable");
    const disable = document.querySelector("#push-disable");
    const test = document.querySelector("#push-test");
    let registration;
    let subscription;

    function displayPush(message) {
        status.textContent = message;
        enable.hidden = Boolean(subscription) || Notification.permission === "denied";
        disable.hidden = !subscription;
        test.hidden = !subscription;
    }

    async function requestPush(url, method, value) {
        const response = await fetch(url, {
            method,
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content,
            },
            body: JSON.stringify(value.toJSON()),
        });
        if (response.status === 204) return;
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error || "No se pudo completar la acción. Reabre la aplicación e inténtalo de nuevo.");
    }

    function applicationKey(key) {
        const padded = key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4);
        return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
    }

    workerReady.then(async (worker) => {
        if (!worker?.pushManager || !("Notification" in window)) {
            status.textContent = "Abre la aplicación instalada desde el icono de Romina para activar los avisos.";
            return;
        }
        registration = worker;
        subscription = await worker.pushManager.getSubscription();
        displayPush(
            Notification.permission === "denied" ? "Los avisos están bloqueados. Puedes permitirlos en los ajustes del teléfono."
                : subscription ? "Avisos activados en este teléfono." : "Puedes activar los avisos en este teléfono."
        );
    }).catch(() => { status.textContent = "El teléfono no pudo comprobar los avisos."; });

    enable.addEventListener("click", async () => {
        enable.disabled = true;
        let pending;
        try {
            const permission = await Notification.requestPermission();
            if (permission !== "granted") {
                displayPush(permission === "denied"
                    ? "Los avisos están bloqueados en los ajustes del teléfono."
                    : "El permiso de avisos sigue pendiente.");
                return;
            }
            pending = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: applicationKey(push.dataset.publicKey),
            });
            await requestPush(push.dataset.deviceUrl, "PUT", pending);
            subscription = pending;
            displayPush("Avisos activados en este teléfono.");
        } catch (error) {
            if (pending) await pending.unsubscribe().catch(() => {});
            displayPush(error.message);
        } finally {
            enable.disabled = false;
        }
    });

    disable.addEventListener("click", async () => {
        disable.disabled = true;
        test.disabled = true;
        try {
            await requestPush(push.dataset.deviceUrl, "DELETE", subscription);
            await subscription.unsubscribe();
            subscription = null;
            displayPush("Avisos desactivados en este teléfono.");
        } catch (error) {
            displayPush(error.message);
        } finally {
            disable.disabled = false;
            test.disabled = false;
        }
    });

    test.addEventListener("click", async () => {
        test.disabled = true;
        disable.disabled = true;
        try {
            await requestPush(push.dataset.testUrl, "POST", subscription);
            displayPush("El servicio de avisos aceptó la prueba. Revisa las notificaciones de tu teléfono.");
        } catch (error) {
            displayPush(error.message);
        } finally {
            test.disabled = false;
            disable.disabled = false;
        }
    });
}
