/* 极简 Service Worker。
 *
 * 故意只做两件事：让"添加到主屏幕"能拿到可安装资格、断网时给一个壳。
 * 不做离线消息、不做后台同步 —— 聊天站离线没有意义，做多了只会让更新卡住。
 *
 * 三条纪律：
 *   1. 绝不缓存 /api/*（缓存了会看到别人或过期的回复）
 *   2. 静态资源 network-first（宁可慢一点，也不要发版后用户还在用旧代码）
 *   3. 只对页面导航回退到 index，别把 HTML 塞给图片请求
 */

const VERSION = 'umf-v1';
const SHELL = ['/', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/sw.js') return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && url.pathname !== '/') {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(request);
        if (cached) return cached;
        // 只有"打开页面"这种导航请求才回退到壳，其余请求如实失败
        if (request.mode === 'navigate') return caches.match('/');
        return Response.error();
      }),
  );
});
