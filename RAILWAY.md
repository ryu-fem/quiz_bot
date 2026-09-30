# نشر البوت على Railway

## قبل ما تبدأ
- اعمل `/revoke` للتوكن من @BotFather وخد توكن جديد (التوكن القديم اتسرّب في الـ logs).
- **اقفل البوت اللي على جهازك** (وخدمة systemd لو شغّلتها: `systemctl --user disable --now quiz-bot`). نسختين بنفس التوكن = خطأ `Conflict`.
- الملفات الجاهزة: `Dockerfile` و`railway.json` (بيعيد التشغيل تلقائيًا لو البوت وقع) و`.gitignore` (بيمنع رفع `.env`).

## الطريقة 1: عن طريق GitHub (الأسهل)
1. اعمل repo **Private** على GitHub وارفع فيه: `bot.py`, `requirements.txt`, `Dockerfile`, `railway.json`, `.gitignore`. **مترفعش `.env`.**
2. في railway.com: New Project ← Deploy from GitHub repo ← اختار الـ repo.
3. ادخل على الـ service ← تبويب **Variables** وضيف:

| المتغير | القيمة |
|---|---|
| `BOT_TOKEN` | التوكن الجديد |
| `ADMIN_IDS` | الـ id بتاعك |
| `GROQ_API_KEY` | مفتاح Groq |
| `CEREBRAS_API_KEY` | مفتاح Cerebras (احتياطي) |
| `TARGET_CHAT` | `@اسم_القناة` (مكان النشر) |
| `DEFAULT_PREFIX` | اختياري، مثلًا `🔥 Grammar` |
| `OPEN_ACCESS` | اختياري. البوت مفتوح لأي حد افتراضيًا، اكتب `false` عشان يبقى للأدمن بس |

4. Railway هيبني ويشغّل لوحده. افتح تبويب **Deployments ← View Logs**، وأول سطر لازم يقولك أنهي مزودين اتفعلوا. ابعت `/start` للبوت.

## الطريقة 2: من غير GitHub (CLI)
```bash
npm i -g @railway/cli
railway login
cd ~/Desktop/quiz
railway init
railway up
```
وبعدين ضيف الـ Variables من الـ dashboard زي فوق.

## ملاحظات مهمة
- **البوت ملوش منفذ (port) ولا رابط**، ده طبيعي لأنه بيشتغل بالـ polling. متعملش Generate Domain.
- ملف الإعدادات بيتمسح مع كل نشر جديد. عشان كده حط `TARGET_CHAT` و`DEFAULT_PREFIX` في الـ Variables. لو غيّرت `/target` أو `/prefix` من البوت، حدّث الـ Variables كمان. أو ضيف Volume على `/data` وحط `CONFIG_PATH=/data/config.json` فتفضل الإعدادات.
- `/undo` بيشتغل على آخر دفعة بس طول ما البوت ماتعملوش restart.
- اللوج: تبويب Deployments. بعد أي تعديل في الكود ارفعه على GitHub ويتنشر تلقائيًا.

## الوصول للناس
- أي حد يبعت للبوت أسئلة بيرجعله Quiz Polls في شاته هو. **القناة (`/target`) والعنوان (`/prefix`) للأدمن بس** (اللي id بتاعه في `ADMIN_IDS`).
- مفيش حد يومي للرسايل. الحدود الوحيدة: حجم الرسالة الواحدة للمستخدم العادي (`USER_MAX_CHARS`، الافتراضي 15000 حرف)، وعدد طلبات الـ AI في نفس الوقت (`AI_CONCURRENCY`). الأدمن من غير حدود.
- كل الناس بتستهلك مفاتيح الـ AI المجانية بتاعتك، فلو الزحمة زادت الطلبات ممكن تتأخر أو توصل للحد عند المزود.
- لو عايزه ليك بس: `OPEN_ACCESS=false`.

## التكلفة
Railway مفيهاش طبقة مجانية دايمة. المتاح: رصيد تجريبي $5 لمدة 30 يوم، وبعدها خطة Hobby بـ $5 في الشهر بتشمل $5 استخدام. البوت ده خفيف جدًا فغالبًا استهلاكه صغير. المصادر مختلفة في هل التسجيل بيطلب كارت ولا لأ، وفي وجود خطة مجانية بـ $1 في الشهر، فاتأكد من صفحة railway.com/pricing قبل ما تبدأ.
