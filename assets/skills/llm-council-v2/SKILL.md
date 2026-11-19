---
name: llm-council-v2
description: >-
  Multi-role LLM council for refactor/MR readiness reviews. Use when the user
  asks for "konsey", council retrospective, what's missing before merge, shotgun
  surgery audit, or traceability/parity checks on a large in-progress change.
---

# LLM Konseyi v2

Refactor veya büyük WIP değişikliklerinde çok rollü retrospektif çalıştır. Amaç: MR hazırlığını sayısal ve kanıta dayalı değerlendirmek.

## Ne zaman kullan

- Kullanıcı "konsey", "neler eksik", "devam et", "MR hazır mı" der
- Shotgun surgery / tekilleştirme refactor'ı orta veya son aşamada
- Commit öncesi son tur kontrolü

## Zorunlu çıktılar (6 adet)

Her konsey oturumu şunları üretir — eksik biri varsa oturum tamamlanmış sayılmaz:

### 1. Yönetici özeti

Skor tablosu (0–100%): Mimari hedef, shotgun azaltma, test konsolidasyonu, MR hazırlığı, davranış parity.

**MR hazır mı?** Tek cümle: Evet/Hayır + gerekçe.

### 2. Traceability matrisi

| Modül / özellik | Catalog / merkez | Provider / impl | Guard test | Davranış test |
| --------------- | ---------------- | --------------- | ---------- | ------------- |

Boş hücre = backlog maddesi (P1/P2/P3).

### 3. Parity sözleşmesi

Bilinçli davranış değişiklikleri tablo halinde:

| Eski | Yeni | Test | QA onayı |
| ---- | ---- | ---- | -------- |

Yeşil test olmadan bilinçli değişiklik merge önerilmez.

### 4. Rol bazlı değerlendirme

Her rol için: Tamamlanan (dosya/test kanıtı) · Eksikler · Veto var mı?

| Rol     | Veto kriteri                                           |
| ------- | ------------------------------------------------------ |
| Mimari  | Yeni kod merkezi catalog/abstraction'ı bypass ediyor   |
| QA      | Guard veya parity testi eksik                          |
| Bakım   | Aynı mantık 2+ yerde kopyalandı                        |
| Release | Untracked dosya, kırmızı CI, stage edilmemiş yeni bean |

**Tek veto = tur durur; önce o madde kapanır.**

### 5. Öncelikli backlog

```text
P1 — MR blocker (untracked, compile, scope test kırmızı)
P2 — Test/parity gap (duplicate guard, eksik version test)
P3 — Sağlamlaştırma (ArchUnit, CSV mapping, docs)
P4 — Sonraki faz (context slim, phase-2)
```

Mermaid flowchart isteğe bağlı; backlog liste zorunlu.

### 6. Verification gate sonuçları

```bash
# Gate A — scope (her tur sonu, zorunlu)
cd <repo> && mvn test -Dtest='<scope-pattern>'

# Gate B — tam modül (MR öncesi)
cd <repo> && mvn test

# Gate C — diff audit (MR öncesi)
git diff --stat && git status -s | grep '^??' || true
```

Gate C'de `??` varsa **MR hazır değil**.

## Konsey prompt şablonu

Kullanıcı yeni konsey istediğinde bu yapıyı doldur:

```markdown
LLM Konseyi v2 — [proje / adım]

Bağlam: [branch, ticket, hedef]
Diff: [+/- satır, untracked sayısı]

[Mimari / QA / Bakım / Release bölümleri]

Traceability matrisi: ...
Parity tablosu: ...
Backlog P1–P4: ...
Gate A/B/C: ...
Karar: MR hazır mı?
```

## Anti-pattern kaydı (refactor sonrası güncelle)

| Yapma                                    | Bunun yerine                                         |
| ---------------------------------------- | ---------------------------------------------------- |
| Provider testinde `@SpringBootTest`      | Mockito + `@InjectMocks`                             |
| Her provider testinde `isExecutable`     | Tek `*CatalogGuardTest`                              |
| Versiyon = ayrı top-level `@Component`   | Nested inner class                                   |
| Kill-switch `Set.of` manuel              | Catalog'dan türet                                    |
| Version logic büyük context sınıfında    | `*Transformations` util                              |
| Paket move + catalog refactor aynı MR    | İki MR: önce davranış, sonra `git mv` only           |
| `DeviceModuleNames` pwifi/catalog'a taşı | Kök `utils`'te bırak (InformListener paylaşıyor)     |
| ArchUnit sadece catalog uyumu için       | `PwifiModuleCatalogGuardTest` (mevcut JUnit yeterli) |

## Oturum sonrası

Kullanıcı "devam et" derse: P1 backlog'u uygula, Gate A koştur, kısa durum özeti ver. Commit/MR yalnızca kullanıcı açıkça isterse.

**Skill güncelleme kuralı:** Her konsey oturumunda yeni kararlar, veto'lar, anti-pattern'ler ve proje-özel notlar bu dosyaya eklenir. Paket taşıma kararı ertelendiyse bile `Phase 2 backlog` bölümüne yazılır.

## Paket / dizin taşıması (ayrı MR — davranış refactor ile karıştırma)

| Kural        | Açıklama                                                                                                      |
| ------------ | ------------------------------------------------------------------------------------------------------------- |
| Timing       | Catalog/refactor MR merge sonrası **move-only** ikinci MR                                                     |
| Release veto | Move + logic aynı MR'da ise **veto**                                                                          |
| Runtime      | Spring `com.kaizen.cloud` scan; `DataProviderFactory` `getName()` kullanır — paket değişikliği bean'i kırmaz |
| İsim alanı   | `service.pwifi` = domain servisleri; `dataprovider.pwifi` = firmware param provider'ları (farklı katman)      |

### Hedef yapı (AAS Phase 2 — onaylı, henüz uygulanmadı)

```text
dataprovider/pwifi/
├── PwifiDataProvider.java, AbstractPwifiS3DataProvider.java
├── catalog/     → PwifiModuleCatalog, PwifiKillSwitchPolicy
├── profiles/    → AbstractQoe*, QoeOrchestratorProfiles*, ProfileTransformations
├── AppQoeDataProvider, QoeFlowstatsDataProvider, Latency*, FingerPrint*, QoeOrchestrator*
├── AirMapQos, AirWanMonitoring, LatencyKillSwitchFirmwareParams, S3ProviderConfig
calculator/, cache/, utils/DeviceModuleNames → kökte kalır (paylaşımlı)
S3DataProvider → domain-agnostic, pwifi dışı
```

Önceden var örnek: `dataprovider/ssidprofiles/`.

## AAS P-WiFi WIP durum takibi (son konsey snapshot)

| Metrik       | Değer                                               |
| ------------ | --------------------------------------------------- |
| Diff         | ~45 dosya, net −1454 satır                          |
| Untracked    | 13 dosya (yeni catalog + nested provider + testler) |
| Gate A       | dataprovider + utils + MeshProtocolQueryTests           |
| MR hazır     | Hayır — Gate C: 13 `??`                             |
| Paket taşıma | Phase 2, **başlanmadı**                             |

### Parity notları (AAS)

| Versiyon      | Davranış                      | Test                       |
| ------------- | ----------------------------- | -------------------------- |
| V12 profil    | VPN_RA her zaman çıkar        | ProfileTransformationsTest |
| V13 profil    | VPN_RA korunur                | ProfileTransformationsTest |
| V14 profil    | OBP=true sabit provider'da    | applyV14 obpEnabled test   |
| V14 OBP false | Transformation util destekler | applyV14 obpDisabled test  |

### P1 backlog (aktif)

- 13 untracked dosyayı stage et (commit kullanıcı isteyince)
- [x] `service-query-module-paths.csv` → `qoe-orchestrator-profiles,1.4` satırı

### P3 backlog

- ~~ArchUnit: `PwifiDataProvider` zorunluluğu~~ **İptal** — `PwifiModuleCatalogGuardTest` + `PwifiModuleCatalogTest` yeterli; yeni bağımlılık yok
- Tam `mvn test` bean çakışması (`autoProvisioningByServiceQueryService`) — refactor dışı

## AAS P-WiFi referansı (örnek scope)

Gate A örnek pattern:

```bash
mvn test -Dtest='com.kaizen.cloud.aas.service.em.sq.dataprovider.**.*Test,com.kaizen.cloud.aas.service.em.sq.dataprovider.utils.**.*Test,MeshProtocolQueryTests'
```

Merkez dosyalar: `PwifiModuleCatalog`, `PwifiDataProvider`, `PwifiModuleCatalogGuardTest`, `ProfileTransformations`.
