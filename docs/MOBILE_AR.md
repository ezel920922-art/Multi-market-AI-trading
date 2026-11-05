# تطبيق الهاتف — Android APK وPWA

- واجهة الهاتف الأساسية هي /desk مع شريط تنقل سفلي ومراعاة Safe Areas.
- PWA: manifest.webmanifest وservice-worker.js وأيقونات SVG.
- Service Worker يخزن غلاف الواجهة فقط؛ لا يخزن أي استجابة من /api/ أو /paper/ أو /risk/.
- Android: غلاف Capacitor يفتح واجهة الإنتاج عبر HTTPS. لا توجد صلاحية تداول حقيقي أو صلاحيات وسيط.
- GitHub Actions يبني Debug APK عند الدفع إلى فرع feat/ai-swarm-desk أو عند التشغيل اليدوي، ويحتفظ بالـartifact لمدة 14 يوماً.
- APK الحالي مخصص للاختبار والتثبيت اليدوي، وليس إصدار متجر Google Play موقّعاً.
- يجب نشر تحديث الواجهة على Render بعد موافقة المالك حتى تظهر نسخة الهاتف الجديدة في التطبيق.
