// Keep the app installable without caching mileage, location, login, or report data.
self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

// Take over newly opened installed-app windows as soon as this worker is active.
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Always use the network response so private trip data is not stored by this worker.
self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});
