self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(clients.claim()));
self.addEventListener('push', event => {
  let id = '';
  try { id = event.data.json().notification_id || ''; } catch (_) {}
  const valid = /^[a-f0-9]{32}$/.test(id);
  const url = '/?screen=guardian-view' + (valid ? '&notification=' + id : '');
  event.waitUntil(self.registration.showNotification('귀가 ON', {
    body: '새 보호자 알림이 있어요. 사이트에서 확인해 주세요.',
    icon: '/static/logo.svg', tag: valid ? 'comeback-' + id : 'comeback-guardian', data: {url}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const candidate = event.notification.data?.url || '';
  const url = /^\/\?screen=guardian-view(?:&notification=[a-f0-9]{32})?$/.test(candidate) ? candidate : '/?screen=guardian-view';
  event.waitUntil(clients.openWindow(url));
});
