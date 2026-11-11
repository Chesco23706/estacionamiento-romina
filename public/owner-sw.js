"use strict";

self.addEventListener("push", (event) => {
    let message;
    try { message = event.data?.json(); } catch { message = null; }
    event.waitUntil(self.registration.showNotification(message?.title || "Romina", {
        body: message?.body || "Hay una actualización en la aplicación.",
        icon: "/owner-192.png",
        tag: message?.tag || "romina",
        data: message?.url || "/owner",
    }));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    event.waitUntil((async () => {
        const target = new URL(event.notification.data || "/owner", self.location.origin);
        if (target.origin !== self.location.origin || target.pathname !== "/owner") return;
        const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        const existing = windows.find((client) => {
            const path = new URL(client.url).pathname;
            return path === "/owner" || path === "/owner/settings";
        });
        if (existing) {
            const navigated = await existing.navigate(target.href).catch(() => null);
            if (navigated) return navigated.focus();
        }
        return self.clients.openWindow(target.href);
    })());
});
