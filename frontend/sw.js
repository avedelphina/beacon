// Service worker: shows Beacon's push notices (backend/push.py) and opens the
// page when one is tapped. Nothing else. No offline cache, because every view
// is live fleet state that a stale copy would misreport.

self.addEventListener("push", (event) => {
  // The payload is only a title that Beacon wrote. iOS ends the subscription
  // if a push shows nothing, so a missing payload still gets a notice.
  let title = "Beacon has news for you";
  try { title = event.data?.json().title || title; } catch { /* not JSON: keep the default */ }
  event.waitUntil(self.registration.showNotification(title, { icon: "/icon-192.png" }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (open.length) return open[0].focus();
    return self.clients.openWindow("/");
  })());
});
