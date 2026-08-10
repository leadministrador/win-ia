// ============================================================
// LEA WIN IA — recibe los avisos aunque la app esté cerrada.
// Este archivo lo instala el navegador y queda corriendo aparte.
// ============================================================

self.addEventListener('install', (evento) => {
  self.skipWaiting();          // que la versión nueva tome control enseguida
});

self.addEventListener('activate', (evento) => {
  evento.waitUntil(self.clients.claim());
});

// Llega un aviso del servidor
self.addEventListener('push', (evento) => {
  let datos = {};
  try {
    datos = evento.data ? evento.data.json() : {};
  } catch (e) {
    datos = { titulo: 'LEA WIN IA', cuerpo: (evento.data && evento.data.text()) || '' };
  }

  const titulo = datos.titulo || 'LEA WIN IA';
  const opciones = {
    body: datos.cuerpo || '',
    icon: '/static/icono-192.png',
    badge: '/static/icono-badge.png',
    tag: datos.etiqueta || 'lea',
    renotify: true,
    vibrate: [180, 80, 180],
    data: { url: datos.url || '/' },
    actions: [{ action: 'abrir', title: 'Ver la carrera' }],
  };

  evento.waitUntil(self.registration.showNotification(titulo, opciones));
});

// El usuario toca el aviso
self.addEventListener('notificationclick', (evento) => {
  evento.notification.close();
  const destino = (evento.notification.data && evento.notification.data.url) || '/';

  evento.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((ventanas) => {
        // Si la app ya está abierta, se usa esa ventana.
        for (const v of ventanas) {
          if ('focus' in v) {
            v.navigate(destino);
            return v.focus();
          }
        }
        // Si no, se abre una nueva.
        if (self.clients.openWindow) {
          return self.clients.openWindow(destino);
        }
      })
  );
});
