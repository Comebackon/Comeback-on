self.addEventListener('push', event => {
  event.waitUntil(self.registration.showNotification('귀가 ON', {
    body: '새 보호자 알림이 있어요. 사이트에서 확인해 주세요.',
    icon: '/static/logo.svg', tag: 'comeback-guardian', data: {url: '/'}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.openWindow('/'));
});
