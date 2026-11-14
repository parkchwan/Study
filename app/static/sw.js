// 공유 받기: 다른 앱에서 '공유 → 시각자료 색인'으로 보낸 글자·사진을 받아 검색 화면으로 넘김.
// (NAS 웹 서버는 POST를 받을 수 없으므로, 이 서비스 워커가 기기 안에서 가로채 처리)
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "POST" || !url.pathname.endsWith("/share")) return;   // 그 밖의 요청은 그대로
  event.respondWith((async () => {
    const form = await event.request.formData();
    const text = [form.get("title"), form.get("text"), form.get("url")].filter(Boolean).join(" ").trim();
    const file = form.get("image");
    const box = await caches.open("share-inbox");
    await box.delete("shared-image");
    if (file && file.size) await box.put("shared-image", new Response(file, { headers: { "Content-Type": file.type || "image/jpeg" } }));
    const base = url.pathname.replace(/share$/, "");
    const qs = new URLSearchParams({ share: file && file.size ? "image" : "text" });
    if (text) qs.set("text", text.slice(0, 500));
    return Response.redirect(`${base}?${qs}`, 303);
  })());
});
