// AIR-AM service worker — מינימלי בכוונה.
// מטרתו היחידה: לעמוד בתנאי ההתקנה כ-PWA ("הוסף למסך הבית"). הוא *לא* מטמיין
// (cache) דבר: זהו כלי LAN שמתעדכן ע"י install.sh, וקאש היה מגיש UI ישן אחרי
// עדכון. ה-handler מחזיר את הבקשה לרשת כברירת מחדל (בלי respondWith), כך
// שהסטרים מ-Icecast (פורט אחר) והקריאות ל-API תמיד טריים.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => { /* passthrough: ברירת מחדל = רשת */ });
