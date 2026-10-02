# SKMR bağımsız doğrulama ve düzeltme raporu

Tarih: 2026-09-26T15:27:32+00:00. Ortam: `wsl -d kali-linux -u root`, Claude Code 2.1.283.
Kapsam: `/root/.claude/skmr`, etkin `bin/lib` hookları, ayarlar, namespace command/skill dosyaları, canonical writer, AgentComm ve mevcut güvenlik/Profile regresyonları.

Önceki tamamlanma iddiası bütünüyle doğru değildi. İlk mevcut runner 6/7 paket gösterirken, aktif host testleri dahil edildiğinde atlanmış eski sözleşme hataları da açığa çıktı. Yeni bağımsız testler gerçek davranış hatalarını üretti; düzeltmeler ardından aynı testlerle ve tam regression turuyla doğrulandı.

## Son sonuç

- **11/11 test paketi geçti.** Runner artık peer staging kopyasını değil güncel host dosyalarını çalıştırıyor.
- **43/43 yeni audit regression testi geçti.** Bağımsız test dosyası `skmr/tests/test_audit_regressions.py`.
- **43/43 mevcut §90 mekanik kontrolü geçti.** Test açıklamasıyla actual assertion kapsamı farklı olan eski probe’lar düzeltildi; yorum/docstring eşleşmeleri başarı veya hata sayılmıyor.
- **107/107 davranış sözleşmesi korundu.** CLAUDE.md 23.042 → 16.450 bayt; Profile/transport kuralları Claude’un otomatik yüklediği `rules/` dosyalarında korunuyor. Toplam otomatik yüklenen kural metni yaklaşık 23 KB; bu değişiklik toplam context’te aynı oranda azalma iddiası değildir.
- Canlı index: **49 dosya, 261 chunk, 261 vector**; bge-m3 kurulu ve kullanılabilir. Tokenizer değişikliği için index sürümü 2 yapıldı ve index yeniden üretildi. Sonraki sync **0 chunk / 0 embedding** üretti.
- Canlı AgentComm HTTP/LAN health yanıtları başarılı. Gerçek bir Sondra mesajı gönderilmedi. Ulaşılamayan transport, geçici ve sentetik config ile **exit=3** kontrollü hatası olarak ayrıca doğrulandı.

## Düzeltilenler

- Çift Planning çıktısı, trivial işte büyük plan, namespace çözümleme ve planlama hatasının dispatcher dışına taşması.
- Gerçekte başlamamış ajanları “called” diye bildirme. Planlama artık adayları açıkça **planned (not started)** diye gösteriyor.
- Banner-only Skill hook yerine geçerli Claude hook JSON’u; UserPromptSubmit state okuma ve SessionStart kalıcı identity context’i.
- Gerçek ajan başlangıç/bitiş kaydı, planned/actual agent eşlemesi, bağımlılık kapısı, döngü/duplicate ID kontrolü, aktif işi ezmeme, illegal result transition kontrolü.
- Tekrarlı RUNNING milestone’unun kaybolması; sonuç JSON’unda eşzamanlı yazı kaybı; MEMORY tablosunda pipe/yeni satırın kolonları bozması; agent detaylarının eksikliği.
- Türkçe/Unicode BM25 tokenları; eski model vektörlerinin aynı boyutta olunca yanlış karşılaştırılması; config/model ve chunk boyutu değişiminde stale provider/index.
- Vektör sorgusu ortasında hata olunca BM25 fallback’in atlanması; BM25 hata olunca vector fallback’in atlanması; sağlıklı “eşleşme yok” durumunu global outage diye bildirme.
- Profile seçeneklerini tek query string’e gömme, negatif/eksik search limit/mode flag’i, grep query’nin option/regex sanılması.
- `/skmr:send` içinde bilinmeyen peer ismini sessizce gerçek peer’e yönlendirme; role kontrol karakterleri, uzun hostname label’ları, eksik remote flag’in topology’yi silmesi; role update hatasında yarım kalıcı yazı.
- Doctor’ın validator `ok:false` içeriğini kontrol etmeden başarı bildirmesi.
- HackerOne’ın eski vendor dataset’ini kullanması; “latest” için tarihe göre sıralamaması; filtre seçeneklerini query’ye katması.
- Learning adayının olmayan explicit user request’i uydurması; commit için candidate path yerine preview token kullanılması gerektiği.
- Tests’in live config/MEMORY/state’i değiştirmesi; güncel writer routing/backlink/full-merge sözleşmesine uymayan Profile fixture’ları; geliştirme skill’ini security sanan eski inventory testi.

## Çalışan akış ve bağımlılıklar

`/skmr:* → cli.py → registry/dispatcher → execution → planning → handler → completion/telemetry`.
`bin/skmr-runtime.py → hooks/runtime.py → planner + native/topology + orchestrator`.
Claude’un Agent tool’u gerçek dispatch’i yapar. SKMR hook’u tek başına bir LLM/CLI process’i spawn etmez; planı ana modele verir, gerçek Agent/Subagent olaylarını izler ve bağımlı başlatmaları engeller.
`retrieval → BM25/vector → normalized weighted veya RRF → feature rerank → dedupe`; incremental index SQLite’ta. Kalıcı vault yazısı canonical writer’ın guarded preview→commit akışında kalır.

Import haritası `dependency-map.json`; değişen dosya hash’leri `changes.json`. Legacy lifecycle entrypoint, mevcut test/import tüketicileri nedeniyle ince bir compatibility adapter olarak korundu; ayrı lifecycle implementation’ı yok.

## Sınırlar ve kalan doğrulama

- Yeni hooklar script seviyesinde gerçek payload şekilleriyle test edildi. **Yeni bir gerçek Claude model oturumunun bu planı takip edip ajan dispatch etmesini uçtan uca çalıştırmadım.** Ajan çıktısındaki iddialar bağımsız kanıt olmadan `verified` yapılmıyor. Bu yüzden tüm maddeleri koşulsuz “%100 kanıtlandı” diye işaretlemiyorum.
- Identity/state detection ve Profile-scoped reader test edildi; modelin kullanıcı kimliğini okuyup doğru konuşma yanıtı vermesi ayrıca gözlenmedi.
- Otomatik knowledge/error candidate filtreleri ve save gate test edildi. Bir modelin her durumda doğru kalıcı dersi seçmesi deterministik scriptlerle garanti edilemez.
- Canlı Sondra teslim/alındı/cevap roundtrip testi yapılmadı; servis health ve mevcut AgentComm regresyonları geçti.
- Eski Claude işlem sırasını ve tüm eski arşivlerin geçmiş temizliğini mevcut dosyalardan kesin olarak kanıtlamak mümkün değil. Önceki arşivleri ve yedekleri silmedim.

Yeni bir Claude oturumunda `/skmr:doctor`, “Nerede kalmıştık?” ve küçük bir izinli Agent işi yeni kancaların oturuma yüklenmesini doğrular. Hook sözleşmesi [resmî Claude Code hooks referansı](https://code.claude.com/docs/en/hooks) ile kontrol edildi; exec-form `args` mevcut sürümde desteklendiği için doğru ayarlar korunmuştur.

## 0–92 madde bazlı kontrol

“Doğrulandı” ilgili kod/CLI/script kanıtını ifade eder; modelin serbest akıl yürütmesi için mutlak garanti değildir. Numara ve başlıklar kullanıcının verdiği listeden alınmıştır.

| # | Madde | Durum | Kanıt / sınır |
|---|---|---|---|
| 0 | PRIMARY OBJECTIVE | Doğrulandı | Etkin dosyalar ve çağrı/import haritası incelendi; dependency-map.json üretildi. Eski arşiv ve yedekler korunarak gereksiz importlar ve üretilmiş paket önbellekleri temizlendi. |
| 1 | NON-NEGOTIABLE RULES | Doğrulandı | Etkin dosyalar ve çağrı/import haritası incelendi; dependency-map.json üretildi. Eski arşiv ve yedekler korunarak gereksiz importlar ve üretilmiş paket önbellekleri temizlendi. |
| 2 | REQUIRED IMPLEMENTATION ORDER | Geriye dönük tam ispat yok | Önceki Claude çalışmasının bütün aşamaları hangi sırada yaptığı yalnızca mevcut dosyalardan kanıtlanamaz. Bu denetimde önce analiz/reference map, sonra başarısız test, düzeltme ve regression sırası uygulandı. |
| 3 | INITIAL REPOSITORY ANALYSIS | Doğrulandı | Etkin dosyalar ve çağrı/import haritası incelendi; dependency-map.json üretildi. Eski arşiv ve yedekler korunarak gereksiz importlar ve üretilmiş paket önbellekleri temizlendi. |
| 4 | CLEANUP — UNUSED / BROKEN / LEGACY CODE | Etkin kapsam doğrulandı | Etkin SKMR kaynakları, entrypoint/hook bağımlılıkları ve importlar incelendi. Eski arşivdeki 28 dosyanın geçmişte nasıl reference-check edildiği bağımsız olarak yeniden ispatlanmadı; rastgele eski /root dosyaları silinmedi. |
| 5 | SKMR COMMAND NAMESPACE | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 6 | CENTRAL COMMAND DISPATCHER | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 7 | SKILL REGISTRY | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 8 | GLOBAL SKILL EXECUTION LIFECYCLE | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 9 | EXECUTION HOOK | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 10 | AUTOMATIC PLANNING HOOK | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 11 | PLANNING DEPTH | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 12 | SUBAGENT DECISION SYSTEM | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 13 | SUBAGENT COUNT | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 14 | SUBAGENT PLAN OUTPUT | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 15 | SUBAGENT IDENTIFIERS | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 16 | SUBAGENT STATE MACHINE | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 17 | SUBAGENT DEPENDENCIES | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 18 | SUBAGENT RESULT COLLECTION | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 19 | SUBAGENT VERIFICATION | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 20 | MEMORY ARCHITECTURE | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 21 | NATIVE STATE MEMORY — MEMORY.md | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 22 | MEMORY.md EXAMPLE | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 23 | SUBAGENT STATE MUST BE STORED IN MEMORY.md | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 24 | SUBAGENT PROGRESS UPDATES | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 25 | AUTOMATIC STATE MEMORY ROUTING | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 26 | INTENT DETECTION | Algılama/kimlik kaydı doğrulandı; model yanıtı sınanmadı | TR/EN state ve identity intent, kısa ifadeler, anlamsal fallback hata kontrolü ve kalıcı agent topology doğrulandı. Kullanıcı kimliği için kanonik Profile kaynağına yönlendirme var; modelin o kaynağı okuyarak yanıtlaması canlı oturumda ayrıca sınanmadı. |
| 27 | IDENTITY MEMORY | Algılama/kimlik kaydı doğrulandı; model yanıtı sınanmadı | TR/EN state ve identity intent, kısa ifadeler, anlamsal fallback hata kontrolü ve kalıcı agent topology doğrulandı. Kullanıcı kimliği için kanonik Profile kaynağına yönlendirme var; modelin o kaynağı okuyarak yanıtlaması canlı oturumda ayrıca sınanmadı. |
| 28 | OBSIDIAN MEMORY | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 29 | OBSIDIAN MEMORY SEARCH MODES | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 30 | BM25 | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 31 | VECTOR SEARCH | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 32 | HYBRID SEARCH | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 33 | DO NOT NAIVELY ADD BM25 + VECTOR SCORES | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 34 | CANDIDATE RETRIEVAL | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 35 | RERANKING | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 36 | CHUNKING | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 37 | EMBEDDING MODEL | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 38 | EMBEDDING PROVIDER ABSTRACTION | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 39 | INCREMENTAL INDEXING | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 40 | EMBEDDING VERSION INVALIDATION | Doğrulandı | Canlı bge-m3; BM25/vector/hybrid; RRF/normalize weighted; başlıklı chunking, dedupe/rerank, provider, SQLite index ve sürüm invalidation doğrulandı. Sentetik robot kolu sorusu farklı ifadelerle doğru nota döndü. Değişmeyen dosyalar 0 yeniden embedding üretti. |
| 41 | OBSIDIAN COMMAND | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 42 | MEMORY WRITE DECISION SYSTEM | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 43 | KNOWLEDGE LEARNING SYSTEM | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 44 | ERROR LEARNING | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 45 | ERROR MEMORY LOCATION | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 46 | ROUTING SYSTEM | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 47 | AGENTCOMM | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 48 | ASSIGN ROLE SYSTEM | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 49 | REMOTE AGENT CONFIGURATION | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 50 | ROLE VALIDATION | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 51 | AGENT RELATIONSHIPS | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 52 | CLAUDE.md ROLE UPDATE | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 53 | CLAUDE.md REFACTOR | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 54 | ROLE STATE UPDATE | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 55 | ASSIGN ROLE OUTPUT | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 56 | HACKERONE REPORTS | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 57 | BUGSKILLS-AI | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 58 | COMMAND DISCOVERY | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 59 | STANDARD OUTPUT FORMAT | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 60 | EXAMPLE FULL EXECUTION | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 61 | MEMORY.md DURING EXECUTION | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 62 | STATE UPDATE POLICY | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 63 | STATE WRITE SAFETY | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 64 | SINGLE SOURCE OF TRUTH | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 65 | CONFIGURATION | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 66 | SEARCH FALLBACK | Doğrulandı | Ollama/vektör kesintisi, BM25 kesintisi, bozuk index, eksik vault/MEMORY, hatalı config, skill/subagent failure ve sentetik ulaşılamayan AgentComm endpointi kontrollü işlendi. Gerçek bge-m3 lexical/semantic/hybrid sorguları ve incremental/version testleri geçti. |
| 67 | MEMORY SYSTEM INDEPENDENCE | Doğrulandı | Ollama/vektör kesintisi, BM25 kesintisi, bozuk index, eksik vault/MEMORY, hatalı config, skill/subagent failure ve sentetik ulaşılamayan AgentComm endpointi kontrollü işlendi. Gerçek bge-m3 lexical/semantic/hybrid sorguları ve incremental/version testleri geçti. |
| 68 | SEARCH RESULT QUALITY | Doğrulandı | Ollama/vektör kesintisi, BM25 kesintisi, bozuk index, eksik vault/MEMORY, hatalı config, skill/subagent failure ve sentetik ulaşılamayan AgentComm endpointi kontrollü işlendi. Gerçek bge-m3 lexical/semantic/hybrid sorguları ve incremental/version testleri geçti. |
| 69 | MEMORY WRITE FORMAT | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 70 | DIRECTORY STRUCTURE | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 71 | BACKWARD COMPATIBILITY | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 72 | TESTING — COMMANDS | Doğrulandı | 10 /skmr:* komutu ve registry eşleşiyor; merkezi dispatcher, alias, CLI, namespace, hata ve mevcut servis testleri geçiyor. |
| 73 | TESTING — EXECUTION HOOK | Doğrulandı | Tek merkezi banner/planning pass, NO_COLOR, kısa plan ve karmaşık plan testleri geçti; plan komutunun çift Planning çıktısı giderildi. |
| 74 | TESTING — PLANNING HOOK | Doğrulandı | Tek merkezi banner/planning pass, NO_COLOR, kısa plan ve karmaşık plan testleri geçti; plan komutunun çift Planning çıktısı giderildi. |
| 75 | TESTING — SUBAGENTS | Mekanizma doğrulandı; canlı dispatch sınanmadı | Göreve göre aday ajan sayısı, benzersiz ID, gerçek SubagentStart/Stop, bağımlılık kapısı, durum geçişleri, sonuç toplama, duplicate bulgu ve unverified işaretleri sınandı. Yalnızca planlanan ajanlar “başlatıldı” diye bildirilmiyor; gerçek model dispatch davranışı için canlı oturum gerekir. |
| 76 | TESTING — SUBAGENT MEMORY | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 77 | TESTING — NATIVE MEMORY | Doğrulandı | Native MEMORY.md ile uzun vadeli Obsidian ayrımı; gerçek state okuma, kilitli/atomik yazma, eşzamanlı sonuçlar, durum tablosu, detaylar, blocker/next ve yeni okumayla geri kazanım testleri geçti. |
| 78 | TESTING — IDENTITY | Algılama/kimlik kaydı doğrulandı; model yanıtı sınanmadı | TR/EN state ve identity intent, kısa ifadeler, anlamsal fallback hata kontrolü ve kalıcı agent topology doğrulandı. Kullanıcı kimliği için kanonik Profile kaynağına yönlendirme var; modelin o kaynağı okuyarak yanıtlaması canlı oturumda ayrıca sınanmadı. |
| 79 | TESTING — OBSIDIAN | Doğrulandı | Ollama/vektör kesintisi, BM25 kesintisi, bozuk index, eksik vault/MEMORY, hatalı config, skill/subagent failure ve sentetik ulaşılamayan AgentComm endpointi kontrollü işlendi. Gerçek bge-m3 lexical/semantic/hybrid sorguları ve incremental/version testleri geçti. |
| 80 | TESTING — INDEX | Doğrulandı | Ollama/vektör kesintisi, BM25 kesintisi, bozuk index, eksik vault/MEMORY, hatalı config, skill/subagent failure ve sentetik ulaşılamayan AgentComm endpointi kontrollü işlendi. Gerçek bge-m3 lexical/semantic/hybrid sorguları ve incremental/version testleri geçti. |
| 81 | TESTING — ASSIGN ROLE | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 82 | FAILURE TESTS | Doğrulandı | Ollama/vektör kesintisi, BM25 kesintisi, bozuk index, eksik vault/MEMORY, hatalı config, skill/subagent failure ve sentetik ulaşılamayan AgentComm endpointi kontrollü işlendi. Gerçek bge-m3 lexical/semantic/hybrid sorguları ve incremental/version testleri geçti. |
| 83 | REGRESSION TEST | Doğrulandı | 11/11 güncel test paketi, 43 yeni audit regression testi, 43/43 §90 probe ve 107/107 policy coverage. README ve bu madde bazlı rapor güncellendi; geçici state/vault fixture’ları izole edildi. |
| 84 | FINAL CLEANUP | Doğrulandı | 11/11 güncel test paketi, 43 yeni audit regression testi, 43/43 §90 probe ve 107/107 policy coverage. README ve bu madde bazlı rapor güncellendi; geçici state/vault fixture’ları izole edildi. |
| 85 | DOCUMENTATION | Doğrulandı | 11/11 güncel test paketi, 43 yeni audit regression testi, 43/43 §90 probe ve 107/107 policy coverage. README ve bu madde bazlı rapor güncellendi; geçici state/vault fixture’ları izole edildi. |
| 86 | DO NOT DO THESE | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 87 | CORE ARCHITECTURAL PRINCIPLES | Kod/gate doğrulandı; modelin ders seçimi sınanmadı | Knowledge/error filtreleri ve kanonik preview→commit korunuyor. Otomatik aday hiçbir açık kullanıcı talebi uyduramıyor. Staging kalıcı kayıt sayılmıyor. Modelin kendiliğinden doğru dersi seçmesi scriptlerle bütünüyle ispatlanamaz. |
| 88 | EXPECTED FINAL ARCHITECTURE | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |
| 89 | AGENT TOPOLOGY | Doğrulandı | Local/remote role akışı, boş/eksik alanlar, IP/hostname/role validation, duplicate/cycle ilişkiler, delimited CLAUDE.md/topology güncellemesi, store hatasında rollback ve state güncellemesi test edildi. 107/107 davranış sözleşmesi korundu. |
| 90 | FINAL VERIFICATION CHECKLIST | Doğrulandı | 11/11 güncel test paketi, 43 yeni audit regression testi, 43/43 §90 probe ve 107/107 policy coverage. README ve bu madde bazlı rapor güncellendi; geçici state/vault fixture’ları izole edildi. |
| 91 | COMPLETION RULE | Doğrulandı | 11/11 güncel test paketi, 43 yeni audit regression testi, 43/43 §90 probe ve 107/107 policy coverage. README ve bu madde bazlı rapor güncellendi; geçici state/vault fixture’ları izole edildi. |
| 92 | FINAL BEHAVIOR EXPECTATION | Kanca/CLI doğrulandı; model davranışı sınanmadı | Merkezi execution/planning/routing ve runtime hook kurulu. Skill yüklenmesi işin tamamlanması sayılmıyor. Kanca olayları test edildi; gerçek model oturumunun planı uygulaması ayrıca gözlenmedi. |

## Yedek ve tekrar çalıştırma

Değişen mevcut dosyaların yedeği: `/root/.claude/backups/skmr-codex-audit-20260926T150724Z`. Credentials/config tokenları rapora taşınmadı.
Tam test turu: `/usr/bin/python3 /root/.claude/skmr/tests/run_all.py`.
Bağımsız audit suite: `/usr/bin/python3 /root/.claude/skmr/tests/test_audit_regressions.py`.
Geçici fixture/state dosyaları live bellek yerine izole dizinlerde tutulur.

## Tam test paketi çıktısı

```text
[PASS] system: 5.87s
[PASS] failures: 1.69s
[PASS] index: 1.69s
[PASS] policy coverage: 0.02s
[PASS] audit regressions: 0.48s
[PASS] agentcomm: 0.83s
[PASS] test_skmr_profile_hardening: 0.20s
[PASS] test_skmr_profiles: 0.66s
[PASS] test_skmr_regressions: 1.25s
[PASS] test_skmr_security_registry: 1.60s
[PASS] test_skmr_vnext: 0.94s
11/11 suites passed
```
